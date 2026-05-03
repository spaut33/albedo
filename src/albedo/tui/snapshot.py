"""Aggregate state for the TUI.

The supervisor process drives a Rich Live display by loading this snapshot
on every refresh tick (~250 ms). The snapshot collects:

* per-worker status from `state/agent-<id>.status.json`
* heartbeat freshness for liveness markers
* a ring-buffered tail of recent log events (across all workers)
* (later phases) Linear queue counts, project stats, alerts

Heavy reads (whole log files) happen only on first load — subsequent ticks
seek from the last byte offset. Status files are small enough to re-read
in full each time.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from albedo.heartbeat import (
    DEFAULT_STALE_AFTER_SECONDS,
    heartbeat_path,
    is_stale,
    last_heartbeat,
)
from albedo.status_writer import (
    PHASE_POLLING,
    WorkerStatus,
    read_status,
    status_path,
)

EVENTS_RING_SIZE = 200
LOG_GLOB = 'logs/agent-*.log'
LINEAR_QUEUE_FILE = 'linear_queue.json'


@dataclass(slots=True)
class LogEvent:
    ts: float
    agent: str
    level: str
    message: str

    @property
    def hms(self) -> str:
        return time.strftime('%H:%M:%S', time.localtime(self.ts))


def _empty_counts() -> dict[str, int]:
    return {}


@dataclass(slots=True)
class LinearQueueSnapshot:
    counts: dict[str, int] = field(default_factory=_empty_counts)
    updated_at: float = 0.0


@dataclass(slots=True)
class WorkerView:
    agent_id: str
    status: WorkerStatus | None
    heartbeat_age_seconds: float | None
    is_stale: bool


@dataclass(slots=True)
class AggregatedSnapshot:
    workers: list[WorkerView]
    events: list[LogEvent]
    linear_queue: LinearQueueSnapshot
    project_name: str
    now: float


class LogTailer:
    """Streams new lines from rotating per-agent JSON log files.

    Keeps a (inode, offset) marker per path. Rotation is detected by inode
    change: the next read starts from byte 0. Lines with bad JSON are
    dropped silently.
    """

    def __init__(self, log_dir: Path, ring_size: int = EVENTS_RING_SIZE) -> None:
        self._log_dir = log_dir
        self._offsets: dict[Path, tuple[int, int]] = {}
        self._buffer: deque[LogEvent] = deque(maxlen=ring_size)
        # Seed with end-of-file so the TUI doesn't replay history on launch.
        for path in self._discover_paths():
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            self._offsets[path] = (stat.st_ino, stat.st_size)

    def poll(self) -> list[LogEvent]:
        """Read new lines from every known log file. Returns appended events."""
        appended: list[LogEvent] = []
        for path in self._discover_paths():
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            inode, last_offset = self._offsets.get(path, (stat.st_ino, 0))
            if inode != stat.st_ino:
                last_offset = 0
            if stat.st_size < last_offset:
                last_offset = 0
            try:
                with path.open('rb') as f:
                    f.seek(last_offset)
                    data = f.read()
                    new_offset = f.tell()
            except OSError:
                continue
            self._offsets[path] = (stat.st_ino, new_offset)
            for raw in data.splitlines():
                if not raw.strip():
                    continue
                event = _parse_log_line(raw, fallback_agent=_agent_from_log_path(path))
                if event is None:
                    continue
                self._buffer.append(event)
                appended.append(event)
        return appended

    def recent(self, n: int) -> list[LogEvent]:
        if n <= 0:
            return []
        if n >= len(self._buffer):
            return list(self._buffer)
        # `deque` slicing is O(n); n is small (≤ panel height).
        return list(self._buffer)[-n:]

    def _discover_paths(self) -> list[Path]:
        if not self._log_dir.exists():
            return []
        return sorted(self._log_dir.glob('agent-*.log'))


def _agent_from_log_path(path: Path) -> str:
    name = path.stem  # agent-1, agent-2
    return name.removeprefix('agent-')


def _parse_log_line(raw: bytes, *, fallback_agent: str) -> LogEvent | None:
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    data = cast('dict[str, Any]', parsed)
    ts_raw = data.get('timestamp')
    ts = _parse_iso_timestamp(ts_raw) if isinstance(ts_raw, str) else time.time()
    agent = str(data.get('agent') or fallback_agent or '?')
    level = str(data.get('level', 'info'))
    message = str(data.get('event', ''))
    return LogEvent(ts=ts, agent=agent, level=level, message=message)


def _parse_iso_timestamp(value: str) -> float:
    """Parse the structlog ISO timestamp (UTC, no tz suffix or 'Z')."""
    cleaned = value.rstrip('Z')
    if cleaned.endswith('+00:00'):
        cleaned = cleaned[:-6]
    # Manual parse: 2026-05-02T09:52:28.662942
    try:
        from datetime import UTC, datetime

        return datetime.fromisoformat(cleaned).replace(tzinfo=UTC).timestamp()
    except ValueError:
        return time.time()


def load_linear_queue(state_dir: Path) -> LinearQueueSnapshot:
    path = state_dir / LINEAR_QUEUE_FILE
    try:
        raw = path.read_text(encoding='utf-8')
    except (FileNotFoundError, OSError):
        return LinearQueueSnapshot()
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        return LinearQueueSnapshot()
    if not isinstance(parsed, dict):
        return LinearQueueSnapshot()
    data = cast('dict[str, Any]', parsed)
    counts_raw = data.get('counts', {})
    counts: dict[str, int] = {}
    if isinstance(counts_raw, dict):
        counts_dict = cast('dict[str, Any]', counts_raw)
        for k, v in counts_dict.items():
            if isinstance(v, int) and not isinstance(v, bool):
                counts[str(k)] = v
    updated_at_raw = data.get('updated_at')
    updated_at = (
        float(updated_at_raw)
        if isinstance(updated_at_raw, int | float)
        and not isinstance(updated_at_raw, bool)
        else 0.0
    )
    return LinearQueueSnapshot(counts=counts, updated_at=updated_at)


def load_worker_views(
    state_dir: Path,
    agent_ids: list[str],
    *,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> list[WorkerView]:
    views: list[WorkerView] = []
    now = time.time()
    for agent_id in agent_ids:
        status = read_status(status_path(state_dir, agent_id))
        hb_path = heartbeat_path(state_dir, agent_id)
        hb = last_heartbeat(hb_path)
        age = (now - hb.timestamp()) if hb is not None else None
        stale = is_stale(hb_path, max_age_seconds=stale_after_seconds)
        views.append(
            WorkerView(
                agent_id=agent_id,
                status=status,
                heartbeat_age_seconds=age,
                is_stale=stale and status is not None,
            )
        )
    return views


def discover_agent_ids(state_dir: Path, expected: int | None = None) -> list[str]:
    """Best-effort enumeration of worker agent ids.

    Looks at status files first (workers that already booted), then heartbeat
    files (workers that haven't published status yet). If `expected` is set,
    pads the list up to that count so the TUI can show "—" rows for slots
    that haven't started writing yet.
    """
    ids: set[str] = set()
    for path in state_dir.glob('agent-*.status.json'):
        ids.add(_agent_id_from_path(path, suffix='.status.json'))
    for path in state_dir.glob('agent-*.heartbeat'):
        ids.add(_agent_id_from_path(path, suffix='.heartbeat'))
    if expected is not None:
        for n in range(1, expected + 1):
            ids.add(str(n))
    return sorted(ids, key=_id_sort_key)


def _agent_id_from_path(path: Path, *, suffix: str) -> str:
    name = path.name
    if name.endswith(suffix):
        name = name[: -len(suffix)]
    return name.removeprefix('agent-')


def _id_sort_key(agent_id: str) -> tuple[int, str]:
    try:
        return (int(agent_id), agent_id)
    except ValueError:
        return (1_000_000, agent_id)


def aggregate(
    *,
    state_dir: Path,
    log_tailer: LogTailer,
    expected_workers: int | None,
    project_name: str,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    events_limit: int = 60,
) -> AggregatedSnapshot:
    log_tailer.poll()
    ids = discover_agent_ids(state_dir, expected=expected_workers)
    workers = load_worker_views(state_dir, ids, stale_after_seconds=stale_after_seconds)
    queue = load_linear_queue(state_dir)
    return AggregatedSnapshot(
        workers=workers,
        events=log_tailer.recent(events_limit),
        linear_queue=queue,
        project_name=project_name,
        now=time.time(),
    )


def humanize_phase(phase: str) -> str:
    """Compact label for the workers table — keeps columns narrow."""
    if not phase:
        return PHASE_POLLING
    return phase.replace('_', ' ')
