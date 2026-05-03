"""Minimal GitHub REST client for the orchestrator's housekeeping needs.

Currently a single read-only method: `get_pull_request`. We do NOT use this
client for opening PRs or posting reviews — that work happens inside
`claude -p` through the GitHub MCP. The orchestrator only needs to *check*
PR state to drive the merge → Done automation.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class PullRequest:
    owner: str
    repo: str
    number: int
    state: str  # 'open' | 'closed'
    merged: bool


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
        """Return the PR's current state. Raises `GithubError` on HTTP failure."""
        path = f'/repos/{owner}/{repo}/pulls/{number}'
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = self._client.get(path)
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
                raise GithubError(f'PR {owner}/{repo}#{number} not found')
            if response.status_code >= 400:
                raise GithubError(
                    f'GitHub HTTP {response.status_code}: {response.text}'
                )
            body = response.json()
            return PullRequest(
                owner=owner,
                repo=repo,
                number=number,
                state=str(body.get('state', '')),
                merged=bool(body.get('merged', False)),
            )
        raise GithubError(
            f'GitHub request failed after {self._max_retries} attempts: {last_error}'
        )

    def _sleep(self, attempt: int) -> None:
        time.sleep(self._backoff_seconds * (attempt + 1))
