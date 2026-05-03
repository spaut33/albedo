"""Tests for the minimal GitHub REST client."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from albedo.github_client import (
    GithubClient,
    GithubError,
    PullRequest,
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


def test_get_pull_request_raises_on_404() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text='not found')

    with (
        _make_client(handler) as client,
        pytest.raises(GithubError, match='not found'),
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
