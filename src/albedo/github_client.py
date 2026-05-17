"""Minimal GitHub REST client for the orchestrator's housekeeping needs.

Originally a single read-only PR check, the client now also exposes the
workflow-run / job / log endpoints needed by AI-56's CI-redispatch flow.
We still do NOT use this client for opening PRs or posting reviews —
that work happens inside `claude -p` through the GitHub MCP. The
orchestrator only needs to *check* state to drive automation.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, cast

import httpx
from pydantic import SecretStr

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_BASE_URL = 'https://api.github.com'
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_PR_URL_PATTERN = re.compile(
    r'https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)'
)


class GithubError(RuntimeError):
    """Raised when GitHub returns a non-2xx response or other transport error."""


class GithubNotFoundError(GithubError):
    """Raised on a 404 — callers can catch this to skip missing resources."""


@dataclass(frozen=True, slots=True)
class PullRequest:
    owner: str
    repo: str
    number: int
    state: str  # 'open' | 'closed'
    merged: bool
    head_branch: str = ''


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    name: str
    number: int
    conclusion: str | None


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    id: int
    name: str
    status: str
    conclusion: str | None
    html_url: str
    head_sha: str


@dataclass(frozen=True, slots=True)
class WorkflowJob:
    id: int
    name: str
    conclusion: str | None
    steps: tuple[WorkflowStep, ...]


@dataclass(frozen=True, slots=True)
class PullRequestReview:
    """A PR-level review summary (the `/pulls/{n}/reviews` shape).

    `state` is GitHub's review event: typically `APPROVED`,
    `CHANGES_REQUESTED`, `COMMENTED`, or `DISMISSED`. Albedo's reviewer
    always posts with `state == 'COMMENTED'` and signals the verdict in
    `body` via a `VERDICT: APPROVE|REQUEST_CHANGES` marker — see
    `worker.VERDICT_PATTERN`.
    """

    id: int
    user_login: str
    state: str
    body: str
    submitted_at: str
    commit_id: str


@dataclass(frozen=True, slots=True)
class PullRequestReviewComment:
    """A single line-anchored review comment (the `/pulls/{n}/comments` shape).

    `line` is the GitHub side `line` field when present, falling back to
    `original_line` so a comment anchored on a now-outdated diff line is
    still rendered with its original position.
    """

    id: int
    user_login: str
    path: str
    line: int | None
    body: str
    commit_id: str
    pull_request_review_id: int | None


def parse_pr_url(url: str) -> tuple[str, str, int] | None:
    """Extract `(owner, repo, number)` from a GitHub PR URL or None."""
    match = _PR_URL_PATTERN.match(url.strip())
    if match is None:
        return None
    return match.group('owner'), match.group('repo'), int(match.group('number'))


class GithubClient:
    """Tiny GitHub REST client with PAT auth and bounded retry."""

    def __init__(
        self,
        pat: SecretStr,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
    ) -> None:
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._client = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=timeout_seconds,
            headers={
                'Accept': 'application/vnd.github+json',
                'Authorization': f'Bearer {pat.get_secret_value()}',
                'X-GitHub-Api-Version': '2022-11-28',
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GithubClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_authenticated_login(self) -> str:
        """Return the login for the user the PAT belongs to.

        Used by `preflight` to confirm GitHub credentials work without
        making assumptions about specific repo access. Raises
        `GithubError` on any non-2xx response.
        """
        response = self._client.get('/user')
        if response.status_code >= 400:
            raise GithubError(f'GitHub HTTP {response.status_code}: {response.text}')
        body = response.json()
        login = body.get('login')
        if not isinstance(login, str) or not login:
            raise GithubError('GitHub /user response missing login field')
        return login

    def get_pull_request(self, owner: str, repo: str, number: int) -> PullRequest:
        """Return the PR's current state.

        Raises `GithubNotFoundError` when the PR is missing,
        `GithubError` on any other failure.
        """
        path = f'/repos/{owner}/{repo}/pulls/{number}'
        not_found = f'PR {owner}/{repo}#{number} not found'
        response = self._request('GET', path, not_found_message=not_found)
        body = response.json()
        return PullRequest(
            owner=owner,
            repo=repo,
            number=number,
            state=str(body.get('state', '')),
            merged=bool(body.get('merged', False)),
        )

    def list_pull_requests(
        self, owner: str, repo: str, *, state: str = 'open'
    ) -> list[PullRequest]:
        """Return PRs for the repo, scoped by `state` (default 'open').

        The list endpoint omits the `merged` flag, so callers that need
        merge status must follow up with `get_pull_request`. For our
        CI-redispatch use case `state='open'` is enough — closed/merged
        PRs are simply absent from the cache and skipped.
        """
        path = f'/repos/{owner}/{repo}/pulls'
        not_found = f'Pull requests for {owner}/{repo} not found'
        response = self._request(
            'GET',
            path,
            params={'state': state, 'per_page': 100},
            not_found_message=not_found,
        )
        body = response.json()
        raw_prs = cast('list[dict[str, Any]]', body if isinstance(body, list) else [])
        return [_parse_pull_request(raw, owner, repo) for raw in raw_prs]

    def list_pull_request_reviews(
        self, owner: str, repo: str, number: int
    ) -> list[PullRequestReview]:
        """Return PR reviews (summary level), oldest-first per GitHub.

        Raises `GithubNotFoundError` if the PR is missing, `GithubError`
        on other failures. Empty list when the PR has no reviews yet.
        """
        path = f'/repos/{owner}/{repo}/pulls/{number}/reviews'
        not_found = f'Reviews for {owner}/{repo}#{number} not found'
        response = self._request(
            'GET',
            path,
            params={'per_page': 100},
            not_found_message=not_found,
        )
        body = response.json()
        raw_reviews = cast(
            'list[dict[str, Any]]', body if isinstance(body, list) else []
        )
        return [_parse_pull_request_review(raw) for raw in raw_reviews]

    def list_pull_request_review_comments(
        self, owner: str, repo: str, number: int
    ) -> list[PullRequestReviewComment]:
        """Return all line-anchored review comments on a PR, oldest-first.

        Unlike `list_pull_request_reviews`, GitHub returns every
        line-anchored comment across all reviews here; callers that need
        a specific review's findings filter by `pull_request_review_id`.
        """
        path = f'/repos/{owner}/{repo}/pulls/{number}/comments'
        not_found = f'Review comments for {owner}/{repo}#{number} not found'
        response = self._request(
            'GET',
            path,
            params={'per_page': 100},
            not_found_message=not_found,
        )
        body = response.json()
        raw_comments = cast(
            'list[dict[str, Any]]', body if isinstance(body, list) else []
        )
        return [_parse_pull_request_review_comment(raw) for raw in raw_comments]

    def list_workflow_runs(
        self, owner: str, repo: str, head_branch: str
    ) -> list[WorkflowRun]:
        """Return workflow runs scoped to `head_branch`, newest-first.

        GitHub's `actions/runs` endpoint orders by `created_at desc` by
        default, which matches the newest-first contract. Raises
        `GithubNotFoundError` if the repo or branch query 404s, and
        `GithubError` on other failures.
        """
        path = f'/repos/{owner}/{repo}/actions/runs'
        not_found = f'Workflow runs for {owner}/{repo}@{head_branch} not found'
        response = self._request(
            'GET',
            path,
            params={'branch': head_branch},
            not_found_message=not_found,
        )
        body = response.json()
        raw_runs: list[dict[str, Any]] = body.get('workflow_runs') or []
        return [_parse_workflow_run(run) for run in raw_runs]

    def list_workflow_jobs(
        self, owner: str, repo: str, run_id: int
    ) -> list[WorkflowJob]:
        """Return all jobs for a workflow run, including per-step details.

        Raises `GithubNotFoundError` if the run is missing, and
        `GithubError` on other failures.
        """
        path = f'/repos/{owner}/{repo}/actions/runs/{run_id}/jobs'
        not_found = f'Workflow run {owner}/{repo}#{run_id} not found'
        response = self._request('GET', path, not_found_message=not_found)
        body = response.json()
        raw_jobs: list[dict[str, Any]] = body.get('jobs') or []
        return [_parse_workflow_job(job) for job in raw_jobs]

    def get_job_logs(self, owner: str, repo: str, job_id: int) -> str:
        """Return the raw log text for a workflow job.

        GitHub responds with a 302 redirect to a presigned download URL;
        we follow it transparently. Decodes as utf-8 with replacement so
        a stray non-utf-8 byte never breaks the caller. Raises
        `GithubNotFoundError` if the job has no logs (e.g. expired),
        and `GithubError` on other failures.
        """
        path = f'/repos/{owner}/{repo}/actions/jobs/{job_id}/logs'
        not_found = f'Logs for job {owner}/{repo}#{job_id} not found'
        response = self._request(
            'GET',
            path,
            not_found_message=not_found,
            follow_redirects=True,
        )
        return response.content.decode('utf-8', errors='replace')

    def _request(
        self,
        method: str,
        path: str,
        *,
        not_found_message: str,
        params: dict[str, Any] | None = None,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        """Issue a request with bounded retry on transient errors.

        Retries on transport errors and `RETRYABLE_STATUS` codes up to
        `max_retries` times. 404 maps to `GithubNotFoundError`; other
        4xx and exhausted retries map to `GithubError`.
        """
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = self._client.request(
                    method,
                    path,
                    params=params,
                    follow_redirects=follow_redirects,
                )
            except httpx.HTTPError as exc:
                last_error = exc
                self._sleep(attempt)
                continue
            if response.status_code in RETRYABLE_STATUS:
                last_error = GithubError(
                    f'GitHub returned retryable status {response.status_code}'
                )
                self._sleep(attempt)
                continue
            if response.status_code == 404:
                raise GithubNotFoundError(not_found_message)
            if response.status_code >= 400:
                raise GithubError(
                    f'GitHub HTTP {response.status_code}: {response.text}'
                )
            return response
        raise GithubError(
            f'GitHub request failed after {self._max_retries} attempts: {last_error}'
        )

    def _sleep(self, attempt: int) -> None:
        time.sleep(self._backoff_seconds * (attempt + 1))


def _parse_pull_request(raw: dict[str, Any], owner: str, repo: str) -> PullRequest:
    head_obj = cast('dict[str, Any]', raw.get('head') or {})
    head_branch = str(head_obj.get('ref') or '')
    return PullRequest(
        owner=owner,
        repo=repo,
        number=int(raw.get('number', 0)),
        state=str(raw.get('state', '')),
        merged=bool(raw.get('merged', False)),
        head_branch=head_branch,
    )


def _parse_workflow_run(raw: dict[str, Any]) -> WorkflowRun:
    conclusion = raw.get('conclusion')
    return WorkflowRun(
        id=int(raw.get('id', 0)),
        name=str(raw.get('name', '')),
        status=str(raw.get('status', '')),
        conclusion=str(conclusion) if conclusion is not None else None,
        html_url=str(raw.get('html_url', '')),
        head_sha=str(raw.get('head_sha', '')),
    )


def _parse_workflow_job(raw: dict[str, Any]) -> WorkflowJob:
    conclusion = raw.get('conclusion')
    raw_steps: list[dict[str, Any]] = raw.get('steps') or []
    steps = tuple(_parse_workflow_step(step) for step in raw_steps)
    return WorkflowJob(
        id=int(raw.get('id', 0)),
        name=str(raw.get('name', '')),
        conclusion=str(conclusion) if conclusion is not None else None,
        steps=steps,
    )


def _parse_pull_request_review(raw: dict[str, Any]) -> PullRequestReview:
    user_obj = cast('dict[str, Any]', raw.get('user') or {})
    return PullRequestReview(
        id=int(raw.get('id', 0)),
        user_login=str(user_obj.get('login') or ''),
        state=str(raw.get('state') or ''),
        body=str(raw.get('body') or ''),
        submitted_at=str(raw.get('submitted_at') or ''),
        commit_id=str(raw.get('commit_id') or ''),
    )


def _parse_pull_request_review_comment(
    raw: dict[str, Any],
) -> PullRequestReviewComment:
    user_obj = cast('dict[str, Any]', raw.get('user') or {})
    line_value = raw.get('line')
    if line_value is None:
        line_value = raw.get('original_line')
    parsed_line = line_value if isinstance(line_value, int) else None
    review_id_value = raw.get('pull_request_review_id')
    review_id = int(review_id_value) if isinstance(review_id_value, int) else None
    return PullRequestReviewComment(
        id=int(raw.get('id', 0)),
        user_login=str(user_obj.get('login') or ''),
        path=str(raw.get('path') or ''),
        line=parsed_line,
        body=str(raw.get('body') or ''),
        commit_id=str(raw.get('commit_id') or ''),
        pull_request_review_id=review_id,
    )


def _parse_workflow_step(raw: dict[str, Any]) -> WorkflowStep:
    conclusion = raw.get('conclusion')
    return WorkflowStep(
        name=str(raw.get('name', '')),
        number=int(raw.get('number', 0)),
        conclusion=str(conclusion) if conclusion is not None else None,
    )
