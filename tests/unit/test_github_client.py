"""Tests for the minimal GitHub REST client."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from albedo.github_client import (
    GithubClient,
    GithubError,
    GithubNotFoundError,
    PullRequest,
    PullRequestReview,
    PullRequestReviewComment,
    WorkflowJob,
    WorkflowRun,
    WorkflowStep,
    parse_pr_url,
)


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_retries: int = 3,
) -> GithubClient:
    return GithubClient(
        SecretStr('ghp_test'),
        transport=httpx.MockTransport(handler),
        max_retries=max_retries,
        backoff_seconds=0.0,
    )


def test_parse_pr_url_extracts_owner_repo_number() -> None:
    assert parse_pr_url('https://github.com/me/sample/pull/42') == (
        'me',
        'sample',
        42,
    )
    assert parse_pr_url('  https://github.com/me/sample/pull/3 ') == (
        'me',
        'sample',
        3,
    )


def test_parse_pr_url_returns_none_for_non_pr_url() -> None:
    assert parse_pr_url('not a url') is None
    assert parse_pr_url('https://github.com/me/sample/issues/4') is None


def test_get_pull_request_returns_state_and_merged() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200, json={'state': 'closed', 'merged': True, 'number': 42}
        )

    with _make_client(handler) as client:
        pr = client.get_pull_request('me', 'sample', 42)

    assert pr == PullRequest(
        owner='me', repo='sample', number=42, state='closed', merged=True
    )
    auth = captured[0].headers['authorization']
    assert auth == 'Bearer ghp_test'
    assert captured[0].headers['accept'] == 'application/vnd.github+json'


def test_get_pull_request_raises_typed_not_found_on_404() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text='not found')

    with (
        _make_client(handler) as client,
        pytest.raises(GithubNotFoundError, match='not found'),
    ):
        client.get_pull_request('me', 'sample', 99)


def test_get_pull_request_raises_on_other_4xx() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text='unauthorized')

    with (
        _make_client(handler) as client,
        pytest.raises(GithubError, match='401'),
    ):
        client.get_pull_request('me', 'sample', 1)


def test_get_pull_request_retries_on_5xx() -> None:
    calls = {'n': 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls['n'] += 1
        if calls['n'] < 3:
            return httpx.Response(503, text='busy')
        return httpx.Response(200, json={'state': 'open', 'merged': False})

    with _make_client(handler) as client:
        pr = client.get_pull_request('me', 'sample', 1)
    assert pr.state == 'open' and pr.merged is False
    assert calls['n'] == 3


def test_get_pull_request_gives_up_after_max_retries() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text='busy')

    with (
        _make_client(handler, max_retries=2) as client,
        pytest.raises(GithubError, match='after 2 attempts'),
    ):
        client.get_pull_request('me', 'sample', 1)


def test_get_pull_request_retries_on_transport_error() -> None:
    calls = {'n': 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls['n'] += 1
        if calls['n'] < 2:
            raise httpx.ConnectError('boom')
        return httpx.Response(200, json={'state': 'closed', 'merged': True})

    with _make_client(handler) as client:
        pr = client.get_pull_request('me', 'sample', 1)
    assert pr.merged is True
    assert calls['n'] == 2


def test_list_workflow_runs_returns_typed_objects_in_order() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                'workflow_runs': [
                    {
                        'id': 222,
                        'name': 'CI',
                        'status': 'completed',
                        'conclusion': 'success',
                        'html_url': 'https://github.com/me/sample/actions/runs/222',
                        'head_sha': 'beef',
                    },
                    {
                        'id': 111,
                        'name': 'CI',
                        'status': 'completed',
                        'conclusion': 'failure',
                        'html_url': 'https://github.com/me/sample/actions/runs/111',
                        'head_sha': 'cafe',
                    },
                ]
            },
        )

    with _make_client(handler) as client:
        runs = client.list_workflow_runs('me', 'sample', 'task/foo')

    assert runs == [
        WorkflowRun(
            id=222,
            name='CI',
            status='completed',
            conclusion='success',
            html_url='https://github.com/me/sample/actions/runs/222',
            head_sha='beef',
        ),
        WorkflowRun(
            id=111,
            name='CI',
            status='completed',
            conclusion='failure',
            html_url='https://github.com/me/sample/actions/runs/111',
            head_sha='cafe',
        ),
    ]
    assert captured[0].url.path == '/repos/me/sample/actions/runs'
    assert captured[0].url.params['branch'] == 'task/foo'


def test_list_workflow_runs_handles_null_conclusion_and_missing_list() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                'workflow_runs': [
                    {
                        'id': 1,
                        'name': 'CI',
                        'status': 'in_progress',
                        'conclusion': None,
                        'html_url': 'https://example/1',
                        'head_sha': 'abc',
                    }
                ]
            },
        )

    with _make_client(handler) as client:
        runs = client.list_workflow_runs('me', 'sample', 'main')

    assert len(runs) == 1
    assert runs[0].conclusion is None
    assert runs[0].status == 'in_progress'


def test_list_workflow_runs_raises_typed_not_found_on_404() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text='no such repo')

    with (
        _make_client(handler) as client,
        pytest.raises(GithubNotFoundError, match='not found'),
    ):
        client.list_workflow_runs('me', 'sample', 'task/x')


def test_list_workflow_runs_raises_on_other_4xx() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text='forbidden')

    with (
        _make_client(handler) as client,
        pytest.raises(GithubError, match='403'),
    ):
        client.list_workflow_runs('me', 'sample', 'main')


def test_list_workflow_jobs_returns_jobs_with_steps() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                'jobs': [
                    {
                        'id': 9001,
                        'name': 'lint',
                        'conclusion': 'failure',
                        'steps': [
                            {'name': 'Setup', 'number': 1, 'conclusion': 'success'},
                            {'name': 'Run ruff', 'number': 2, 'conclusion': 'failure'},
                        ],
                    },
                    {
                        'id': 9002,
                        'name': 'test',
                        'conclusion': None,
                        'steps': [],
                    },
                ]
            },
        )

    with _make_client(handler) as client:
        jobs = client.list_workflow_jobs('me', 'sample', 42)

    assert jobs == [
        WorkflowJob(
            id=9001,
            name='lint',
            conclusion='failure',
            steps=(
                WorkflowStep(name='Setup', number=1, conclusion='success'),
                WorkflowStep(name='Run ruff', number=2, conclusion='failure'),
            ),
        ),
        WorkflowJob(id=9002, name='test', conclusion=None, steps=()),
    ]


def test_list_workflow_jobs_raises_typed_not_found_on_404() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text='no such run')

    with (
        _make_client(handler) as client,
        pytest.raises(GithubNotFoundError, match='not found'),
    ):
        client.list_workflow_jobs('me', 'sample', 42)


def test_list_workflow_jobs_raises_on_other_4xx() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text='boom')

    # 500 is retryable; verify a persistent 500 ultimately raises GithubError.
    with (
        _make_client(handler, max_retries=2) as client,
        pytest.raises(GithubError, match='after 2 attempts'),
    ):
        client.list_workflow_jobs('me', 'sample', 42)


def test_get_job_logs_follows_redirect_and_returns_text() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path.endswith('/logs'):
            return httpx.Response(
                302,
                headers={
                    'Location': 'https://logs.example/blob/job-7.txt',
                },
            )
        return httpx.Response(
            200,
            content=b'step 1 ok\nstep 2 boom\n',
        )

    with _make_client(handler) as client:
        text = client.get_job_logs('me', 'sample', 7)

    assert text == 'step 1 ok\nstep 2 boom\n'
    assert seen_paths[0] == '/repos/me/sample/actions/jobs/7/logs'
    # The redirect was actually followed.
    assert any(p.endswith('/blob/job-7.txt') for p in seen_paths)


def test_get_job_logs_decodes_invalid_utf8_with_replacement() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'ok\xff')

    with _make_client(handler) as client:
        text = client.get_job_logs('me', 'sample', 1)

    # The lone 0xff byte is replaced with U+FFFD instead of raising.
    assert text.startswith('ok')
    assert '�' in text


def test_get_job_logs_raises_typed_not_found_on_404() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text='log gone')

    with (
        _make_client(handler) as client,
        pytest.raises(GithubNotFoundError, match='not found'),
    ):
        client.get_job_logs('me', 'sample', 1)


def test_get_job_logs_raises_on_other_4xx() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(410, text='gone')

    with (
        _make_client(handler) as client,
        pytest.raises(GithubError, match='410'),
    ):
        client.get_job_logs('me', 'sample', 1)


def test_list_pull_request_reviews_returns_typed_objects() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    'id': 11,
                    'user': {'login': 'reviewer-bot'},
                    'state': 'COMMENTED',
                    'body': 'shipped\n\nVERDICT: APPROVE',
                    'submitted_at': '2026-05-17T16:15:31Z',
                    'commit_id': 'deadbeef',
                },
                {
                    'id': 12,
                    'user': None,
                    'state': 'COMMENTED',
                    'body': 'nope\n\nVERDICT: REQUEST_CHANGES',
                    'submitted_at': '2026-05-17T17:00:00Z',
                    'commit_id': 'cafebabe',
                },
            ],
        )

    with _make_client(handler) as client:
        reviews = client.list_pull_request_reviews('me', 'sample', 42)

    assert reviews == [
        PullRequestReview(
            id=11,
            user_login='reviewer-bot',
            state='COMMENTED',
            body='shipped\n\nVERDICT: APPROVE',
            submitted_at='2026-05-17T16:15:31Z',
            commit_id='deadbeef',
        ),
        PullRequestReview(
            id=12,
            user_login='',
            state='COMMENTED',
            body='nope\n\nVERDICT: REQUEST_CHANGES',
            submitted_at='2026-05-17T17:00:00Z',
            commit_id='cafebabe',
        ),
    ]
    assert captured[0].url.path == '/repos/me/sample/pulls/42/reviews'


def test_list_pull_request_reviews_raises_typed_not_found_on_404() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text='no such pr')

    with (
        _make_client(handler) as client,
        pytest.raises(GithubNotFoundError, match='not found'),
    ):
        client.list_pull_request_reviews('me', 'sample', 99)


def test_list_pull_request_review_comments_falls_back_to_original_line() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    'id': 100,
                    'user': {'login': 'reviewer-bot'},
                    'path': 'src/x.py',
                    'line': 42,
                    'body': 'rename foo to bar',
                    'commit_id': 'sha1',
                    'pull_request_review_id': 11,
                },
                {
                    'id': 101,
                    'user': {'login': 'reviewer-bot'},
                    'path': 'src/y.py',
                    'line': None,
                    'original_line': 7,
                    'body': 'outdated diff anchor',
                    'commit_id': 'sha2',
                    'pull_request_review_id': 11,
                },
                {
                    'id': 102,
                    'user': {'login': 'reviewer-bot'},
                    'path': 'src/z.py',
                    'line': None,
                    'original_line': None,
                    'body': 'no line at all',
                    'commit_id': 'sha3',
                    'pull_request_review_id': None,
                },
            ],
        )

    with _make_client(handler) as client:
        comments = client.list_pull_request_review_comments('me', 'sample', 42)

    assert comments == [
        PullRequestReviewComment(
            id=100,
            user_login='reviewer-bot',
            path='src/x.py',
            line=42,
            body='rename foo to bar',
            commit_id='sha1',
            pull_request_review_id=11,
        ),
        PullRequestReviewComment(
            id=101,
            user_login='reviewer-bot',
            path='src/y.py',
            line=7,
            body='outdated diff anchor',
            commit_id='sha2',
            pull_request_review_id=11,
        ),
        PullRequestReviewComment(
            id=102,
            user_login='reviewer-bot',
            path='src/z.py',
            line=None,
            body='no line at all',
            commit_id='sha3',
            pull_request_review_id=None,
        ),
    ]


def test_list_pull_request_review_comments_raises_typed_not_found_on_404() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text='no such pr')

    with (
        _make_client(handler) as client,
        pytest.raises(GithubNotFoundError, match='not found'),
    ):
        client.list_pull_request_review_comments('me', 'sample', 99)
