"""History buffers powering the btop-style sparklines.

The TUI calls `HistoryStore.tick(snapshot, ledger)` once per refresh
(~4 Hz). Per-worker activity is appended every tick (cheap: just maps
phase → 0/0.5/1). Token / queue throughput series are bucketed by
minute — the store remembers the last full minute it pushed and only
queries the ledger on minute rollover, so the cost amortises to one
SQL roundtrip per minute regardless of refresh rate.

State is process-local: the store is owned by `TuiApp` and never
serialised; restarting the supervisor wipes history. That's fine for
the current dashboard use case (we already lose log tail offsets too).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from albedo.status_writer import (
    PHASE_RUNNING_CLAUDE,
    PHASE_SPAWNING_CLAUDE,
)
from albedo.tui.snapshot import AggregatedSnapshot

WORKER_ACTIVITY_SAMPLES = 240  # ~60s at 4 Hz
MINUTE_BUCKETS = 60  # one hour of per-minute data
SECONDS_PER_MINUTE = 60
SECONDS_PER_DAY = 86_400


class _LedgerLike(Protocol):
    def stats_window(
        self, *, window_seconds: int = ..., now_unix: int | None = ...
    ) -> object: ...


def _activity_value(phase: str) -> float:
    """Map a worker phase to a 0..1 activity score for sparklines."""
    if phase == PHASE_RUNNING_CLAUDE:
        return 1.0
    if phase == PHASE_SPAWNING_CLAUDE:
        return 0.5
    return 0.0


def _empty_activity() -> dict[str, deque[float]]:
    return {}


def _new_minute_deque() -> deque[int]:
    return deque(maxlen=MINUTE_BUCKETS)


@dataclass(slots=True)
class HistoryStore:
    worker_activity: dict[str, deque[float]] = field(default_factory=_empty_activity)
    token_rate: deque[int] = field(default_factory=_new_minute_deque)
    queue_claims: deque[int] = field(default_factory=_new_minute_deque)
    queue_done: deque[int] = field(default_factory=_new_minute_deque)
    daily_tokens: int = 0
    last_minute_unix: int = 0

    def tick(
        self,
        snapshot: AggregatedSnapshot,
        ledger: _LedgerLike | None,
        *,
        now_unix: int | None = None,
    ) -> None:
        """Append one sample to every series. Cheap on the hot path."""
        for view in snapshot.workers:
            buf: deque[float] | None = self.worker_activity.get(view.agent_id)
            if buf is None:
                buf = deque(maxlen=WORKER_ACTIVITY_SAMPLES)
                self.worker_activity[view.agent_id] = buf
            phase = view.status.phase if view.status is not None else ''
            buf.append(_activity_value(phase))

        now = int(now_unix if now_unix is not None else time.time())
        current_minute = now - (now % SECONDS_PER_MINUTE)
        if self.last_minute_unix == 0:
            # Seed without querying — first tick just anchors the clock so
            # the first real bucket lands on the next minute boundary.
            self.last_minute_unix = current_minute
            return
        if current_minute <= self.last_minute_unix:
            return
        # Could be 1 or many minute boundaries since the last tick (paused
        # terminal, suspended laptop, etc.). Push one bucket per minute so
        # the deque represents real elapsed time.
        missed = (current_minute - self.last_minute_unix) // SECONDS_PER_MINUTE
        if ledger is None:
            for _ in range(missed):
                self.token_rate.append(0)
                self.queue_claims.append(0)
                self.queue_done.append(0)
            self.last_minute_unix = current_minute
            return
        # We only have one snapshot (the last minute); pad the gap with
        # zeros and query for the most recent window. This is a deliberate
        # approximation — backfilling per-minute stats would need a more
        # expensive multi-window query.
        for _ in range(missed - 1):
            self.token_rate.append(0)
            self.queue_claims.append(0)
            self.queue_done.append(0)
        stats = ledger.stats_window(window_seconds=SECONDS_PER_MINUTE, now_unix=now)
        # WindowStats is a frozen dataclass, but we accept duck-typed input
        # for tests — pull attrs through getattr with defaults.
        self.token_rate.append(int(getattr(stats, 'tokens', 0) or 0))
        self.queue_claims.append(int(getattr(stats, 'spawns', 0) or 0))
        self.queue_done.append(int(getattr(stats, 'done', 0) or 0))
        daily = ledger.stats_window(window_seconds=SECONDS_PER_DAY, now_unix=now)
        self.daily_tokens = int(getattr(daily, 'tokens', 0) or 0)
        self.last_minute_unix = current_minute


__all__ = [
    'MINUTE_BUCKETS',
    'WORKER_ACTIVITY_SAMPLES',
    'HistoryStore',
]
