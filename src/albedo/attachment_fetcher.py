"""Download Linear-side attachments into the worktree for the agent.

Two sources are covered:
  * Linear's Attachments API (issue.attachments / comment.attachments).
  * Markdown-embedded `https://uploads.linear.app/...` URLs in the issue
    description and comment bodies — drag-and-dropped images, mostly.

Files land in `<dest_dir>/<id>__<safe>.<ext>` and are listed in the agent
prompt by relative path. The agent reads them via the Read tool, which
is multimodal for images and PDFs (up to 20 pages per call).

The fetcher is idempotent across redispatch: a file with a matching
size on disk is left alone. Attachments outside the configured extension
whitelist are skipped, as are files exceeding `max_file_mb`. Once
`max_files_per_issue` is hit the rest are dropped (logged).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

import httpx
from pydantic import SecretStr

from albedo.config import AttachmentsConfig
from albedo.linear_client import Comment, Issue, RawAttachment

log = logging.getLogger(__name__)

UPLOADS_URL_PATTERN = re.compile(r'https://uploads\.linear\.app/[^\s)\]\'"<>]+')
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 30.0
_FILENAME_SAFE = re.compile(r'[^A-Za-z0-9._-]+')

Origin = Literal['description', 'attachment', 'comment']


@dataclass(frozen=True, slots=True)
class LinearAttachment:
    """A candidate file to download — origin metadata + source URL."""

    id: str
    url: str
    filename: str
    ext: str
    caption: str
    origin: Origin
    origin_detail: str = ''


@dataclass(frozen=True, slots=True)
class FetchedAttachment:
    """A `LinearAttachment` that successfully landed on disk."""

    item: LinearAttachment
    local_path: Path
    bytes: int


@dataclass(frozen=True, slots=True)
class _Limits:
    max_file_bytes: int
    max_files: int
    allowed_extensions: frozenset[str]
    skip_existing: bool = True
    chunk_size: int = 64 * 1024
    timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS


def discover_attachments(
    issue: Issue,
    comments: Sequence[Comment],
) -> list[LinearAttachment]:
    """Collect all candidate attachments from an issue + its comments.

    Order is stable so log output and prompt rendering are deterministic:
    issue-level Attachment-API entries first, then per-comment Attachment
    entries (chronological), then markdown-embedded `uploads.linear.app`
    URLs (description first, then comments). Duplicates by URL are
    dropped — the first occurrence wins.
    """
    items: list[LinearAttachment] = []
    seen: set[str] = set()

    def _push(candidate: LinearAttachment) -> None:
        if candidate.url in seen:
            return
        if not candidate.ext:
            return
        seen.add(candidate.url)
        items.append(candidate)

    for raw in issue.attachments:
        candidate = _from_raw(raw, origin='attachment', origin_detail='issue')
        if candidate is not None:
            _push(candidate)

    for comment in comments:
        for raw in comment.attachments:
            candidate = _from_raw(
                raw, origin='comment', origin_detail=f'comment:{comment.id}'
            )
            if candidate is not None:
                _push(candidate)

    for url in _embedded_urls(issue.description):
        candidate = _from_embedded(url, origin='description', origin_detail='issue')
        if candidate is not None:
            _push(candidate)

    for comment in comments:
        for url in _embedded_urls(comment.body):
            candidate = _from_embedded(
                url, origin='comment', origin_detail=f'comment:{comment.id}'
            )
            if candidate is not None:
                _push(candidate)

    return items


def fetch_attachments(
    items: Sequence[LinearAttachment],
    *,
    dest_dir: Path,
    api_key: SecretStr,
    limits: AttachmentsConfig,
    transport: httpx.BaseTransport | None = None,
) -> list[FetchedAttachment]:
    """Download as many of `items` as the limits allow into `dest_dir`.

    Returns successfully-fetched attachments in input order. Failures
    (network, oversized, unsupported extension) are logged and skipped —
    one bad attachment never aborts the rest. The Linear API key is
    sent as the bare `Authorization` header; if a download is rejected
    with 401/403 we retry once without the header (signed
    `uploads.linear.app` URLs reject auth).
    """
    if not items:
        return []
    dest_dir.mkdir(parents=True, exist_ok=True)
    bound = _Limits(
        max_file_bytes=limits.max_file_mb * 1024 * 1024,
        max_files=limits.max_files_per_issue,
        allowed_extensions=frozenset(
            e.lower().lstrip('.') for e in limits.allowed_extensions
        ),
    )
    fetched: list[FetchedAttachment] = []
    auth_header = {'Authorization': api_key.get_secret_value()}
    with httpx.Client(transport=transport, timeout=bound.timeout_seconds) as client:
        for item in items:
            if len(fetched) >= bound.max_files:
                log.info(
                    'attachments: reached cap of %d, skipping remainder (%d left)',
                    bound.max_files,
                    len(items) - len(fetched),
                )
                break
            if item.ext not in bound.allowed_extensions:
                log.info(
                    'attachments: skip %s — extension %r not in whitelist',
                    item.url,
                    item.ext,
                )
                continue
            target = dest_dir / _safe_name(item)
            existing = _existing_size(target)
            outcome = _download(client, item, target, bound, auth_header, existing)
            if outcome is not None:
                fetched.append(outcome)
    return fetched


def format_attachments_block(
    fetched: Sequence[FetchedAttachment],
    worktree_root: Path,
) -> str:
    """Render the markdown bullet list embedded in the agent prompt.

    Empty when there's nothing to read so the template can `{% if %}` it
    out cleanly. Paths are emitted relative to the worktree root so the
    agent can `Read ./.linear-attachments/...` directly.
    """
    if not fetched:
        return ''
    lines: list[str] = []
    for f in fetched:
        try:
            rel = f.local_path.relative_to(worktree_root)
        except ValueError:
            rel = f.local_path
        origin = _origin_label(f.item)
        caption = f.item.caption.strip()
        suffix = f' — {caption}' if caption else ''
        lines.append(f'- {rel} ({origin}){suffix}')
    return '\n'.join(lines)


def _from_raw(
    raw: RawAttachment, *, origin: Origin, origin_detail: str
) -> LinearAttachment | None:
    if not raw.url:
        return None
    filename, ext = _filename_and_ext(raw.url, fallback_title=raw.title)
    caption = raw.title or raw.subtitle
    return LinearAttachment(
        id=raw.id,
        url=raw.url,
        filename=filename,
        ext=ext,
        caption=caption,
        origin=origin,
        origin_detail=origin_detail,
    )


def _from_embedded(
    url: str, *, origin: Origin, origin_detail: str
) -> LinearAttachment | None:
    filename, ext = _filename_and_ext(url, fallback_title='')
    return LinearAttachment(
        id=_synthetic_id(url),
        url=url,
        filename=filename,
        ext=ext,
        caption='',
        origin=origin,
        origin_detail=origin_detail,
    )


def _embedded_urls(text: str | None) -> Iterable[str]:
    if not text:
        return ()
    return UPLOADS_URL_PATTERN.findall(text)


def _filename_and_ext(url: str, *, fallback_title: str) -> tuple[str, str]:
    parsed = urlparse(url)
    raw_path = unquote(parsed.path)
    name = Path(raw_path).name or fallback_title or 'file'
    suffix = Path(name).suffix.lower().lstrip('.')
    if not suffix and fallback_title:
        suffix = Path(fallback_title).suffix.lower().lstrip('.')
        if suffix:
            name = fallback_title
    return name, suffix


def _safe_name(item: LinearAttachment) -> str:
    base = _FILENAME_SAFE.sub('_', item.filename) or 'file'
    if item.ext and not base.lower().endswith(f'.{item.ext}'):
        base = f'{base}.{item.ext}'
    return f'{item.id}__{base}'


def _existing_size(path: Path) -> int | None:
    if not path.exists():
        return None
    return path.stat().st_size


def _download(
    client: httpx.Client,
    item: LinearAttachment,
    target: Path,
    bound: _Limits,
    auth_header: dict[str, str],
    existing_size: int | None,
) -> FetchedAttachment | None:
    headers = dict(auth_header)
    try:
        outcome = _stream(client, item.url, headers, bound, existing_size)
        if outcome.skip_existing:
            log.debug('attachments: %s already on disk, skipping', target.name)
            return FetchedAttachment(
                item=item, local_path=target, bytes=existing_size or 0
            )
        if outcome.response is None:
            return None
        size = _write_stream(outcome.response, target, bound)
    except httpx.HTTPError as exc:
        log.warning('attachments: download failed for %s: %s', item.url, exc)
        return None
    if size is None:
        return None
    log.info(
        'attachments: fetched %s (%d bytes) from %s',
        target.name,
        size,
        item.origin_detail or item.origin,
    )
    return FetchedAttachment(
        item=replace(item, filename=target.name), local_path=target, bytes=size
    )


@dataclass(frozen=True, slots=True)
class _StreamOutcome:
    """Result of opening a streaming download.

    `response` is the open `httpx.Response` for the caller to drain (and
    must be closed). `skip_existing=True` means the on-disk file already
    matches the server's `Content-Length` and the caller can short-circuit
    without re-downloading. Both fields empty means a hard skip
    (oversized / unauthorized / 4xx).
    """

    response: httpx.Response | None = None
    skip_existing: bool = False


_HARD_SKIP = _StreamOutcome()
_SKIP_EXISTING = _StreamOutcome(skip_existing=True)


def _stream(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    bound: _Limits,
    existing_size: int | None,
) -> _StreamOutcome:
    """Open a streaming GET, retrying without auth on 401/403."""
    attempts: list[dict[str, str]] = [headers]
    if 'Authorization' in headers:
        without_auth = {k: v for k, v in headers.items() if k != 'Authorization'}
        attempts.append(without_auth)
    last: httpx.Response | None = None
    for attempt_headers in attempts:
        request = client.build_request('GET', url, headers=attempt_headers)
        response = client.send(request, stream=True)
        if response.status_code in (401, 403):
            response.close()
            last = response
            continue
        if response.status_code >= 400:
            response.close()
            log.warning('attachments: HTTP %d fetching %s', response.status_code, url)
            return _HARD_SKIP
        declared = _content_length(response)
        if declared is not None and declared > bound.max_file_bytes:
            response.close()
            log.info(
                'attachments: skip %s — declared %d bytes exceeds cap %d',
                url,
                declared,
                bound.max_file_bytes,
            )
            return _HARD_SKIP
        if (
            bound.skip_existing
            and existing_size is not None
            and declared is not None
            and existing_size == declared
        ):
            response.close()
            return _SKIP_EXISTING
        return _StreamOutcome(response=response)
    if last is not None:
        log.warning('attachments: HTTP %d fetching %s', last.status_code, url)
    return _HARD_SKIP


def _write_stream(response: httpx.Response, target: Path, bound: _Limits) -> int | None:
    written = 0
    tmp = target.with_name(target.name + '.part')
    try:
        with tmp.open('wb') as fh:
            for chunk in response.iter_bytes(bound.chunk_size):
                if not chunk:
                    continue
                written += len(chunk)
                if written > bound.max_file_bytes:
                    log.info(
                        'attachments: aborting %s — stream exceeded cap %d',
                        target.name,
                        bound.max_file_bytes,
                    )
                    fh.close()
                    tmp.unlink(missing_ok=True)
                    return None
                fh.write(chunk)
    finally:
        response.close()
    tmp.replace(target)
    return written


def _content_length(response: httpx.Response) -> int | None:
    raw = response.headers.get('content-length')
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _synthetic_id(url: str) -> str:
    """Stable id for a markdown-embedded URL.

    Linear's `uploads.linear.app/<uuid>/<name>` already encodes a UUID;
    if we can find one, use it so re-downloads collide with the same
    on-disk filename. Otherwise fall back to a hash of the URL.
    """
    parsed = urlparse(url)
    parts = [p for p in unquote(parsed.path).split('/') if p]
    for piece in parts:
        if _looks_like_uuid(piece):
            return piece
    import hashlib

    return hashlib.sha1(url.encode('utf-8'), usedforsecurity=False).hexdigest()[:16]


_UUID_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}'
    r'-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)


def _looks_like_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value))


def _origin_label(item: LinearAttachment) -> str:
    if item.origin == 'description':
        return 'embedded in issue description'
    if item.origin == 'attachment':
        return 'issue attachment' if item.origin_detail == 'issue' else 'attachment'
    return f'comment ({item.origin_detail.split(":", 1)[-1]})'
