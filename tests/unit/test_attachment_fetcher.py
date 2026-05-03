"""Tests for the Linear → worktree attachment fetcher.

Mirrors the `httpx.MockTransport` style used in `test_linear_client.py`:
hand-rolled handlers stand in for `uploads.linear.app` and any external
attachment URL, so we exercise the real fetcher without network I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
from pydantic import SecretStr

from albedo.attachment_fetcher import (
    discover_attachments,
    fetch_attachments,
    format_attachments_block,
)
from albedo.config import AttachmentsConfig
from albedo.linear_client import Comment, Issue, RawAttachment

API_KEY = SecretStr('lin_api_test')


def _issue(
    *,
    description: str = '',
    attachments: tuple[RawAttachment, ...] = (),
) -> Issue:
    return Issue(
        id='uuid-1',
        identifier='AI-5',
        title='Add filter',
        description=description,
        state_id='state-backlog',
        state_name='Backlog',
        assignee_id=None,
        label_ids=(),
        label_names=(),
        parent_id=None,
        branch_name='task/ai-5',
        attachments=attachments,
    )


def _comment(
    *,
    body: str = '',
    cid: str = 'c1',
) -> Comment:
    return Comment(
        id=cid,
        body=body,
        author_id='u1',
        created_at='2026-05-02T10:00:00.000Z',
    )


def _limits(**overrides: object) -> AttachmentsConfig:
    base: dict[str, object] = {
        'enabled': True,
        'max_file_mb': 10,
        'max_files_per_issue': 20,
        'allowed_extensions': ('png', 'pdf', 'md', 'txt'),
    }
    base.update(overrides)
    return AttachmentsConfig(**base)  # type: ignore[arg-type]


def _make_handler(
    routes: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url not in routes:
            raise AssertionError(f'unexpected URL fetched: {url}')
        return routes[url](request)

    return handler


def _binary(content: bytes, *, ctype: str = 'image/png') -> httpx.Response:
    return httpx.Response(
        200,
        content=content,
        headers={'content-type': ctype, 'content-length': str(len(content))},
    )


# ---------- discover_attachments ----------


def test_discover_collects_from_all_three_sources_in_stable_order() -> None:
    issue_att = RawAttachment(
        id='att-issue',
        url='https://uploads.linear.app/issue/mock.png',
        title='mock',
    )
    issue = _issue(
        description='see ![inline](https://uploads.linear.app/desc/diag.png)',
        attachments=(issue_att,),
    )
    comments = [_comment(body='attached: https://uploads.linear.app/cbody/notes.md')]
    result = discover_attachments(issue, comments)
    assert [item.url for item in result] == [
        issue_att.url,
        'https://uploads.linear.app/desc/diag.png',
        'https://uploads.linear.app/cbody/notes.md',
    ]
    origins = [item.origin for item in result]
    assert origins == ['attachment', 'description', 'comment']


def test_discover_dedupes_by_url() -> None:
    url = 'https://uploads.linear.app/x/file.png'
    issue = _issue(
        description=f'![]({url})',
        attachments=(RawAttachment(id='att-1', url=url),),
    )
    result = discover_attachments(issue, [_comment(body=url)])
    assert len(result) == 1
    assert result[0].origin == 'attachment'


def test_discover_drops_attachments_without_extension() -> None:
    issue = _issue(
        attachments=(RawAttachment(id='att-1', url='https://example.com/dashboard'),)
    )
    assert discover_attachments(issue, []) == []


# ---------- fetch_attachments ----------


def test_fetch_writes_files_with_authorization_header(tmp_path: Path) -> None:
    captured: list[httpx.Request] = []

    def png_route(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _binary(b'\x89PNG\x00body')

    transport = httpx.MockTransport(
        _make_handler({'https://uploads.linear.app/a/mock.png': png_route})
    )
    items = discover_attachments(
        _issue(
            attachments=(
                RawAttachment(
                    id='att-1',
                    url='https://uploads.linear.app/a/mock.png',
                    title='mock',
                ),
            )
        ),
        [],
    )
    fetched = fetch_attachments(
        items,
        dest_dir=tmp_path,
        api_key=API_KEY,
        limits=_limits(),
        transport=transport,
    )
    assert len(fetched) == 1
    assert fetched[0].local_path.exists()
    assert fetched[0].local_path.read_bytes() == b'\x89PNG\x00body'
    assert captured[0].headers['authorization'] == 'lin_api_test'


def test_fetch_skips_unsupported_extensions(tmp_path: Path) -> None:
    items = list(
        discover_attachments(
            _issue(
                attachments=(
                    RawAttachment(
                        id='zip', url='https://uploads.linear.app/a/dump.zip'
                    ),
                )
            ),
            [],
        )
    )
    transport = httpx.MockTransport(_make_handler({}))  # asserts no fetch
    fetched = fetch_attachments(
        items,
        dest_dir=tmp_path,
        api_key=API_KEY,
        limits=_limits(allowed_extensions=('png', 'pdf', 'md', 'txt')),
        transport=transport,
    )
    assert fetched == []


def test_fetch_rejects_oversized_via_content_length(tmp_path: Path) -> None:
    big = b'x' * 5
    transport = httpx.MockTransport(
        _make_handler(
            {
                'https://uploads.linear.app/a/big.pdf': lambda _r: httpx.Response(
                    200,
                    content=big,
                    headers={'content-length': str(20 * 1024 * 1024)},
                )
            }
        )
    )
    items = discover_attachments(
        _issue(
            attachments=(
                RawAttachment(id='big', url='https://uploads.linear.app/a/big.pdf'),
            )
        ),
        [],
    )
    fetched = fetch_attachments(
        items,
        dest_dir=tmp_path,
        api_key=API_KEY,
        limits=_limits(max_file_mb=1),
        transport=transport,
    )
    assert fetched == []
    assert list(tmp_path.iterdir()) == []


def test_fetch_aborts_when_stream_exceeds_cap_midway(tmp_path: Path) -> None:
    """Server hides size (no Content-Length) and we discover overflow on the wire."""
    transport = httpx.MockTransport(
        _make_handler(
            {
                'https://uploads.linear.app/a/sneaky.pdf': lambda _r: httpx.Response(
                    200, content=b'x' * (2 * 1024 * 1024)
                )
            }
        )
    )
    items = discover_attachments(
        _issue(
            attachments=(
                RawAttachment(
                    id='sneaky', url='https://uploads.linear.app/a/sneaky.pdf'
                ),
            )
        ),
        [],
    )
    fetched = fetch_attachments(
        items,
        dest_dir=tmp_path,
        api_key=API_KEY,
        limits=_limits(max_file_mb=1),
        transport=transport,
    )
    assert fetched == []
    # No partial file is left on disk.
    assert list(tmp_path.iterdir()) == []


def test_fetch_honours_max_files_per_issue(tmp_path: Path) -> None:
    def _route_for(body: bytes) -> Callable[[httpx.Request], httpx.Response]:
        def route(_r: httpx.Request) -> httpx.Response:
            return _binary(body)

        return route

    routes = {
        f'https://uploads.linear.app/a/file{i}.png': _route_for(str(i).encode())
        for i in range(5)
    }
    transport = httpx.MockTransport(_make_handler(routes))
    items = discover_attachments(
        _issue(
            attachments=tuple(
                RawAttachment(id=f'a{i}', url=url) for i, url in enumerate(routes)
            )
        ),
        [],
    )
    fetched = fetch_attachments(
        items,
        dest_dir=tmp_path,
        api_key=API_KEY,
        limits=_limits(max_files_per_issue=2),
        transport=transport,
    )
    assert len(fetched) == 2


def test_fetch_skips_when_existing_file_size_matches(tmp_path: Path) -> None:
    body = b'abc123'
    target_url = 'https://uploads.linear.app/a/idempotent.png'

    calls = {'count': 0}

    def route(request: httpx.Request) -> httpx.Response:
        calls['count'] += 1
        return _binary(body)

    transport = httpx.MockTransport(_make_handler({target_url: route}))
    items = discover_attachments(
        _issue(attachments=(RawAttachment(id='att', url=target_url),)),
        [],
    )

    fetched_first = fetch_attachments(
        items, dest_dir=tmp_path, api_key=API_KEY, limits=_limits(), transport=transport
    )
    fetched_second = fetch_attachments(
        items, dest_dir=tmp_path, api_key=API_KEY, limits=_limits(), transport=transport
    )

    assert len(fetched_first) == 1
    assert len(fetched_second) == 1
    # First call writes; second call sees matching size and skips the body write
    # (response is closed eagerly so no second on-disk overwrite happens).
    assert calls['count'] == 2  # both calls open a stream to compare size headers
    assert fetched_second[0].bytes == len(body)


def test_fetch_retries_without_auth_on_403(tmp_path: Path) -> None:
    target = 'https://uploads.linear.app/a/signed.png'
    seen_auths: list[str | None] = []

    def route(request: httpx.Request) -> httpx.Response:
        seen_auths.append(request.headers.get('authorization'))
        if request.headers.get('authorization'):
            return httpx.Response(403, text='no')
        return _binary(b'ok')

    transport = httpx.MockTransport(_make_handler({target: route}))
    items = discover_attachments(
        _issue(attachments=(RawAttachment(id='att', url=target),)), []
    )
    fetched = fetch_attachments(
        items, dest_dir=tmp_path, api_key=API_KEY, limits=_limits(), transport=transport
    )
    assert len(fetched) == 1
    assert seen_auths == ['lin_api_test', None]


# ---------- format_attachments_block ----------


def test_format_returns_empty_when_nothing_fetched(tmp_path: Path) -> None:
    assert format_attachments_block([], tmp_path) == ''


def test_format_renders_relative_paths_and_origins(tmp_path: Path) -> None:
    target = 'https://uploads.linear.app/a/mock.png'
    items = discover_attachments(
        _issue(
            attachments=(RawAttachment(id='att', url=target, title='login mockup'),)
        ),
        [],
    )
    transport = httpx.MockTransport(_make_handler({target: lambda _r: _binary(b'png')}))
    fetched = fetch_attachments(
        items,
        dest_dir=tmp_path / '.linear-attachments' / 'AI-5',
        api_key=API_KEY,
        limits=_limits(),
        transport=transport,
    )
    block = format_attachments_block(fetched, tmp_path)
    assert block.startswith('- .linear-attachments/AI-5/')
    assert 'issue attachment' in block
    assert 'login mockup' in block
