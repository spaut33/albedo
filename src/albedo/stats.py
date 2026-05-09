"""In-memory session counters and TUI stats summary lines.

`SessionStats` is owned by the supervisor and updated by workers via
filesystem signals (status file outcomes / log events) — but in the
single-process supervisor process the workers run as separate Python
processes, so we don't share the counter directly. Instead the supervisor
re-derives the session totals by querying `usage.db` filtered by
`ts_unix >= supervisor_start`.

This keeps the data path simple: workers only ever write to `usage.db`;
TUI only ever reads from it. No IPC contracts beyond the existing ledger.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from albedo.usage import SECONDS_PER_DAY, UsageLedger, WindowStats


@dataclass(slots=True)
class StatsContext:
    ledger: UsageLedger
    session_started_at_unix: int
    cache_seconds: float = 5.0
    _last_session: tuple[float, WindowStats] | None = None
    _last_daily: tuple[float, WindowStats] | None = None


def session_summary(ctx: StatsContext, *, now_unix: int | None = None) -> str:
    """Compact one-liner for the TUI header, derived from usage.db."""
    now = now_unix if now_unix is not None else int(time.time())
    stats = _cached(
        ctx,
        slot='_last_session',
        window_seconds=now - ctx.session_started_at_unix,
        now_unix=now_unix,
    )
    elapsed = _format_elapsed(now - ctx.session_started_at_unix)
    return f'session {elapsed} · done {stats.done} · fail {stats.failed}'


def daily_summary(ctx: StatsContext, *, now_unix: int | None = None) -> str:
    stats = _cached(
        ctx,
        slot='_last_daily',
        window_seconds=SECONDS_PER_DAY,
        now_unix=now_unix,
    )
    return f'24h: done {stats.done}'


def session_cost_usd(ctx: StatsContext, *, now_unix: int | None = None) -> float:
    now = now_unix if now_unix is not None else int(time.time())
    stats = _cached(
        ctx,
        slot='_last_session',
        window_seconds=now - ctx.session_started_at_unix,
        now_unix=now_unix,
    )
    return float(stats.cost_usd)


def daily_cost_usd(ctx: StatsContext, *, now_unix: int | None = None) -> float:
    stats = _cached(
        ctx,
        slot='_last_daily',
        window_seconds=SECONDS_PER_DAY,
        now_unix=now_unix,
    )
    return float(stats.cost_usd)


def _cached(
    ctx: StatsContext,
    *,
    slot: str,
    window_seconds: int,
    now_unix: int | None,
) -> WindowStats:
    cached = getattr(ctx, slot)
    now = time.monotonic()
    if cached is not None and (now - cached[0]) < ctx.cache_seconds:
        return cached[1]
    fresh = ctx.ledger.stats_window(window_seconds=window_seconds, now_unix=now_unix)
    setattr(ctx, slot, (now, fresh))
    return fresh


def _format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f'{seconds}s'
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f'{minutes}m{secs:02d}s'
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h{minutes:02d}m'
