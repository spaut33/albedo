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

import contextlib
import json
import os
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
SUPERVISOR_LOG_NAME = 'supervisor.log'
SUPERVISOR_AGENT_TOKEN = 'sv'
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

    Holds an open read FD per path so the original inode stays pinned —
    that way a subsequent `unlink + create` at the same path is forced to
    allocate a fresh inode, and rotation is detectable by comparing the
    held FD's inode to `path.stat().st_ino`. Without the open FD, the
    kernel may recycle the inode number (observed on GitHub Actions
    runners), making rotation invisible. Lines with bad JSON are dropped
    silently.
    """

    def __init__(self, log_dir: Path, ring_size: int = EVENTS_RING_SIZE) -> None:
        self._log_dir = log_dir
        # path -> (open fd, inode of the file the fd points to)
        self._handles: dict[Path, tuple[int, int]] = {}
        self._buffer: deque[LogEvent] = deque(maxlen=ring_size)
        # Seed with end-of-file so the TUI doesn't replay history on launch.
        for path in self._discover_paths():
            self._open_handle(path, seek_end=True)

    def _open_handle(self, path: Path, *, seek_end: bool) -> None:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            return
        try:
            ino = os.fstat(fd).st_ino
        except OSError:
            os.close(fd)
            return
        if seek_end:
            try:
                os.lseek(fd, 0, os.SEEK_END)
            except OSError:
                os.close(fd)
                return
        self._handles[path] = (fd, ino)

    def _close_handle(self, path: Path) -> None:
        handle = self._handles.pop(path, None)
        if handle is not None:
            with contextlib.suppress(OSError):
                os.close(handle[0])

    def poll(self) -> list[LogEvent]:
        """Read new lines from every known log file. Returns appended events."""
        appended: list[LogEvent] = []
        for path in self._discover_paths():
            try:
                disk_ino = path.stat().st_ino
            except FileNotFoundError:
                continue
            handle = self._handles.get(path)
            if handle is None or handle[1] != disk_ino:
                # First sighting or rotation: drop the old fd, open the
                # current file from byte 0.
                self._close_handle(path)
                self._open_handle(path, seek_end=False)
                handle = self._handles.get(path)
                if handle is None:
                    continue
            fd = handle[0]
            try:
                stat = os.fstat(fd)
                pos = os.lseek(fd, 0, os.SEEK_CUR)
                if stat.st_size < pos:
                    # In-place truncation — rewind and re-read from 0.
                    os.lseek(fd, 0, os.SEEK_SET)
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                data = b''.join(chunks)
            except OSError:
                continue
            for raw in data.splitlines():
                if not raw.strip():
                    continue
                event = _parse_log_line(raw, fallback_agent=_agent_from_log_path(path))
                if event is None:
                    continue
                self._buffer.append(event)
                appended.append(event)
        return appended

    def close(self) -> None:
        """Release all open file handles. Safe to call multiple times."""
        for path in list(self._handles):
            self._close_handle(path)

    def __del__(self) -> None:  # pragma: no cover - best-effort cleanup
        self.close()

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
        paths: list[Path] = list(self._log_dir.glob('agent-*.log'))
        supervisor = self._log_dir / SUPERVISOR_LOG_NAME
        if supervisor.exists():
            paths.append(supervisor)
        return sorted(paths)


def _agent_from_log_path(path: Path) -> str:
    name = path.stem  # agent-1, agent-2, supervisor
    if name == 'supervisor':
        return SUPERVISOR_AGENT_TOKEN
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
