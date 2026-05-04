"""Detect and persist Claude usage-limit pause windows.

When `claude -p` returns a result like "You've hit your limit · resets 7:40pm
(Europe/Moscow)", every other worker that picks up an issue against the same
upstream account will hit the same wall instantly. This module:

  * parses the reset time and timezone out of the error text,
  * persists the pause window to `state_dir/usage_limit_pause.json`,
  * exposes a read helper the supervisor's poller and TUI consult before
    dispatching new candidates,
  * auto-clears the pause once the wall-clock has caught up.

The pause is process-wide for one supervisor (one state_dir = one Claude
account) — the file is the source of truth, so concurrent workers either
write the same window or observe an already-set one.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

DEFAULT_SAFETY_BUFFER_SECONDS = 60

PAUSE_FILE_NAME = 'usage_limit_pause.json'

# Tolerant pattern: claude-code's text uses "You've hit your limit ·
# resets 7:40pm (Europe/Moscow)" today, but small wording shifts (curly
# apostrophe, "resets at", different separators) shouldn't break parsing.
# The time and tz groups are required; the rest is loose.
_LIMIT_PATTERN = re.compile(
    r"You['’]?ve\s+hit\s+your\s+(?:usage\s+)?limit\s*"  # noqa: RUF001
    r'[·•\-—–:|]?\s*'  # noqa: RUF001
    r'resets?\s+(?:at\s+)?'
    r'(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM)?)'
    r'\s*'
    r'\(\s*(?P<tz>[^)]+?)\s*\)',
    re.IGNORECASE,
)

# Common short-form abbreviations claude-code might emit instead of an
# IANA key. Mapping intentionally small — we only cover what the upstream
# string is likely to print. Anything else falls through to ZoneInfo.
_TZ_ALIASES: dict[str, str] = {
    'UTC': 'UTC',
    'GMT': 'Etc/GMT',
    'PT': 'America/Los_Angeles',
    'PST': 'America/Los_Angeles',
    'PDT': 'America/Los_Angeles',
    'MT': 'America/Denver',
    'MST': 'America/Denver',
    'MDT': 'America/Denver',
    'CT': 'America/Chicago',
    'CST': 'America/Chicago',
    'CDT': 'America/Chicago',
    'ET': 'America/New_York',
    'EST': 'America/New_York',
    'EDT': 'America/New_York',
    'BST': 'Europe/London',
    'CET': 'Europe/Paris',
    'CEST': 'Europe/Paris',
    'MSK': 'Europe/Moscow',
}


@dataclass(frozen=True, slots=True)
class ResetInfo:
    """Parsed reset window (wall clock + tz label + raw match)."""

    until_unix: float
    tz_label: str
    raw_match: str


@dataclass(frozen=True, slots=True)
class PauseState:
    """Persisted pause window observed by all workers + the poller."""

    until_unix: float
    tz_label: str
    reason: str
    set_at_unix: float


def pause_state_path(state_dir: Path) -> Path:
    return state_dir / PAUSE_FILE_NAME


def parse_reset_time(text: str, *, now: datetime | None = None) -> ResetInfo | None:
    """Extract the next reset moment from a claude error string.

    `now` is injected for deterministic tests; defaults to the current
    time in the parsed timezone. Returns None when no match or when the
    timezone is unknown — the caller treats absence as "not a usage-limit
    error" and falls back to normal blocker handling.
    """
    if not text:
        return None
    match = _LIMIT_PATTERN.search(text)
    if match is None:
        return None
    raw_match = match.group(0)
    tz_label = match.group('tz').strip()
    tz = _resolve_timezone(tz_label)
    if tz is None:
        log.warning('usage-limit: unknown timezone %r in %r', tz_label, raw_match)
        return None
    parsed_time = _parse_time_component(match.group('time'))
    if parsed_time is None:
        log.warning('usage-limit: unparseable time in %r', raw_match)
        return None

    now_in_tz = now.astimezone(tz) if now is not None else datetime.now(tz)
    candidate = now_in_tz.replace(
        hour=parsed_time.hour,
        minute=parsed_time.minute,
        second=0,
        microsecond=0,
    )
    if candidate <= now_in_tz:
        candidate += timedelta(days=1)
    return ResetInfo(
        until_unix=candidate.timestamp(),
        tz_label=tz_label,
        raw_match=raw_match,
    )


def _parse_time_component(raw: str) -> time | None:
    cleaned = raw.strip().lower().replace(' ', '')
    suffix = ''
    if cleaned.endswith('am') or cleaned.endswith('pm'):
        suffix = cleaned[-2:]
        cleaned = cleaned[:-2]
    if ':' in cleaned:
        hh_str, mm_str = cleaned.split(':', 1)
    else:
        hh_str, mm_str = cleaned, '0'
    if not hh_str.isdigit() or not mm_str.isdigit():
        return None
    hour = int(hh_str)
    minute = int(mm_str)
    if suffix == 'am' and hour == 12:
        hour = 0
    elif suffix == 'pm' and hour != 12:
        hour += 12
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return time(hour=hour, minute=minute)


def _resolve_timezone(label: str) -> tzinfo | None:
    aliased = _TZ_ALIASES.get(label.upper(), label)
    try:
        return ZoneInfo(aliased)
    except ZoneInfoNotFoundError:
        return None


def write_pause_state(path: Path, state: PauseState) -> None:
    """Atomically persist the pause window (write to .tmp, then os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'until_unix': state.until_unix,
        'tz_label': state.tz_label,
        'reason': state.reason,
        'set_at_unix': state.set_at_unix,
    }
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)


def read_pause_state(path: Path) -> PauseState | None:
    """Load the pause state, returning None on missing/malformed files."""
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding='utf-8')
    except OSError as exc:
        log.warning('usage-limit: read failed at %s: %s', path, exc)
        return None
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning('usage-limit: malformed pause file %s: %s', path, exc)
        return None
    if not isinstance(parsed, dict):
        return None
    data = cast('dict[str, Any]', parsed)
    if 'until_unix' not in data:
        return None
    return PauseState(
        until_unix=float(data['until_unix']),
        tz_label=str(data.get('tz_label', '')),
        reason=str(data.get('reason', '')),
        set_at_unix=float(data.get('set_at_unix', 0.0)),
    )


def clear_pause_state(path: Path) -> None:
    """Best-effort delete of the pause file."""
    if not path.exists():
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning('usage-limit: clear failed at %s: %s', path, exc)


def is_paused(state_dir: Path, *, now_unix: float | None = None) -> PauseState | None:
    """Return the active pause if `now < until + buffer`, else None.

    Side-effect: silently clears the pause file once it has expired so the
    file system never accumulates stale pauses across supervisor restarts.
    """
    path = pause_state_path(state_dir)
    state = read_pause_state(path)
    if state is None:
        return None
    now = now_unix if now_unix is not None else _current_unix()
    if now >= state.until_unix:
        clear_pause_state(path)
        return None
    return state


def record_pause(
    *,
    state_dir: Path,
    info: ResetInfo,
    reason: str,
    now_unix: float | None = None,
    safety_buffer_seconds: int = DEFAULT_SAFETY_BUFFER_SECONDS,
) -> PauseState:
    """Persist a pause window if none exists or the new one extends it.

    Returns the active state — either the freshly written one or the
    existing one if it already covers `info`. This makes concurrent
    writers idempotent: the first to call wins, the rest observe the
    set window without overwriting.
    """
    path = pause_state_path(state_dir)
    now = now_unix if now_unix is not None else _current_unix()
    until = info.until_unix + max(0, safety_buffer_seconds)
    existing = read_pause_state(path)
    if existing is not None and existing.until_unix >= until:
        return existing
    new_state = PauseState(
        until_unix=until,
        tz_label=info.tz_label,
        reason=reason,
        set_at_unix=now,
    )
    write_pause_state(path, new_state)
    return new_state


def _current_unix() -> float:
    """Wall-clock unix timestamp; isolated for test stubbing."""
    return datetime.now().timestamp()


def format_pause_summary(state: PauseState) -> str:
    """Render a one-line operator-facing pause summary.

    Example: "paused until 19:40 Europe/Moscow — claude usage limit".
    """
    tz = _resolve_timezone(state.tz_label) if state.tz_label else None
    if tz is not None:
        local = datetime.fromtimestamp(state.until_unix, tz=tz)
        when = local.strftime('%H:%M')
        label = state.tz_label
    else:
        when = datetime.fromtimestamp(state.until_unix).strftime('%H:%M')
        label = state.tz_label or 'local'
    suffix = f' — {state.reason}' if state.reason else ''
    return f'paused until {when} {label}{suffix}'
