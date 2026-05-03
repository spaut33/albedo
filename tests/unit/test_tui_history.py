"""HistoryStore minute-bucket rollover and worker-activity buffering.

The store is the only stateful piece between TUI ticks; getting bucket
alignment wrong silently flatlines the sparklines, so we cover the edges
explicitly here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from albedo.status_writer import (
    PHASE_POLLING,
    PHASE_RUNNING_CLAUDE,
    PHASE_SPAWNING_CLAUDE,
    WorkerStatus,
)
from albedo.tui.history import (
    MINUTE_BUCKETS,
    SECONDS_PER_DAY,
    SECONDS_PER_MINUTE,
    WORKER_ACTIVITY_SAMPLES,
    HistoryStore,
)
from albedo.tui.snapshot import (
    AggregatedSnapshot,
    LinearQueueSnapshot,
    WorkerView,
)


@dataclass
class _FakeStats:
    tokens: int = 0
    spawns: int = 0
    done: int = 0
    cost_usd: float = 0.0
    wallclock_sec: int = 0
    failed: int = 0


class _FakeLedger:
    """Captures stats_window calls so tests can assert query cadence."""

    def __init__(self, per_window: dict[int, _FakeStats] | None = None) -> None:
        self._per_window = per_window or {}
        self.calls: list[tuple[int, int | None]] = []

    def stats_window(
        self, *, window_seconds: int = SECONDS_PER_DAY, now_unix: int | None = None
    ) -> Any:
        self.calls.append((window_seconds, now_unix))
        return self._per_window.get(window_seconds, _FakeStats())


def _worker(agent_id: str, phase: str) -> WorkerView:
    status = WorkerStatus(
        agent_id=agent_id,
        updated_at=time.time(),
        phase=phase,
        phase_started_at=time.time(),
    )
    return WorkerView(
        agent_id=agent_id,
        status=status,
        heartbeat_age_seconds=0.0,
        is_stale=False,
    )


def _snapshot(workers: list[WorkerView]) -> AggregatedSnapshot:
    return AggregatedSnapshot(
        workers=workers,
        events=[],
        linear_queue=LinearQueueSnapshot(),
        project_name='t',
        now=time.time(),
    )


def test_worker_activity_maps_phase_to_score() -> None:
    store = HistoryStore()
    snap = _snapshot(
        [
            _worker('1', PHASE_RUNNING_CLAUDE),
            _worker('2', PHASE_SPAWNING_CLAUDE),
            _worker('3', PHASE_POLLING),
        ]
    )
    store.tick(snap, ledger=None, now_unix=1000)
    assert list(store.worker_activity['1']) == [1.0]
    assert list(store.worker_activity['2']) == [0.5]
    assert list(store.worker_activity['3']) == [0.0]


def test_worker_activity_buffer_caps_at_max_samples() -> None:
    store = HistoryStore()
    worker = _worker('1', PHASE_RUNNING_CLAUDE)
    snap = _snapshot([worker])
    for i in range(WORKER_ACTIVITY_SAMPLES + 50):
        store.tick(snap, ledger=None, now_unix=1000 + i // 4)
    assert len(store.worker_activity['1']) == WORKER_ACTIVITY_SAMPLES


def test_first_tick_only_anchors_clock_no_ledger_query() -> None:
    """First tick should NOT query the ledger — just seed last_minute_unix."""
    store = HistoryStore()
    ledger = _FakeLedger()
    store.tick(_snapshot([]), ledger=ledger, now_unix=1_000_000)
    assert ledger.calls == []
    assert store.last_minute_unix == 1_000_000 - (1_000_000 % SECONDS_PER_MINUTE)
    assert list(store.token_rate) == []


def test_minute_rollover_appends_one_bucket_and_queries_ledger() -> None:
    store = HistoryStore()
    ledger = _FakeLedger(
        {
            SECONDS_PER_MINUTE: _FakeStats(tokens=1500, spawns=2, done=1),
            SECONDS_PER_DAY: _FakeStats(tokens=42_000),
        }
    )
    base = 1_000_020  # 20s past a minute boundary
    store.tick(_snapshot([]), ledger=ledger, now_unix=base)  # anchor
    store.tick(_snapshot([]), ledger=ledger, now_unix=base + 60)  # rollover

    assert list(store.token_rate) == [1500]
    assert list(store.queue_claims) == [2]
    assert list(store.queue_done) == [1]
    assert store.daily_tokens == 42_000
    # One minute window query + one daily window query.
    assert (SECONDS_PER_MINUTE, base + 60) in ledger.calls
    assert (SECONDS_PER_DAY, base + 60) in ledger.calls


def test_multi_minute_gap_pads_zeros_then_queries_once() -> None:
    """Skipping minutes (paused terminal) shouldn't fan out to many queries."""
    store = HistoryStore()
    ledger = _FakeLedger({SECONDS_PER_MINUTE: _FakeStats(tokens=900, spawns=1, done=1)})
    base = 1_000_000
    store.tick(_snapshot([]), ledger=ledger, now_unix=base)
    store.tick(_snapshot([]), ledger=ledger, now_unix=base + 4 * 60)

    # Three padded zero buckets + one real bucket from the ledger query.
    assert list(store.token_rate) == [0, 0, 0, 900]
    assert list(store.queue_claims) == [0, 0, 0, 1]
    assert list(store.queue_done) == [0, 0, 0, 1]
    # Two real ledger queries (1 min + daily). No per-minute fan-out.
    minute_calls = [c for c in ledger.calls if c[0] == SECONDS_PER_MINUTE]
    assert len(minute_calls) == 1


def test_minute_buckets_capped_at_one_hour() -> None:
    store = HistoryStore()
    ledger = _FakeLedger({SECONDS_PER_MINUTE: _FakeStats(tokens=1)})
    base = 1_000_000
    store.tick(_snapshot([]), ledger=ledger, now_unix=base)
    for i in range(MINUTE_BUCKETS + 10):
        store.tick(_snapshot([]), ledger=ledger, now_unix=base + (i + 1) * 60)
    assert len(store.token_rate) == MINUTE_BUCKETS
    assert len(store.queue_claims) == MINUTE_BUCKETS
    assert len(store.queue_done) == MINUTE_BUCKETS


def test_no_ledger_still_advances_buckets() -> None:
    """A misconfigured app (no ledger) keeps the deques aligned with zeros."""
    store = HistoryStore()
    base = 1_000_000
    store.tick(_snapshot([]), ledger=None, now_unix=base)
    store.tick(_snapshot([]), ledger=None, now_unix=base + 120)
    assert list(store.token_rate) == [0, 0]
    assert store.daily_tokens == 0
