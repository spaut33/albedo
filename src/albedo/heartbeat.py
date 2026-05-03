"""File-based heartbeat for stale-claim recovery.

A worker touches `state/agent-<id>.heartbeat` once per main-loop iteration.
The file's mtime IS the heartbeat — content is irrelevant. On startup the
orchestrator (or another worker) compares mtimes against a wall-clock
threshold; anything older than `STALE_AFTER_SECONDS` is treated as a worker
that died, and its issues are un-claimed (see `housekeeping.py`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

DEFAULT_STALE_AFTER_SECONDS = 5 * 60


def heartbeat_path(state_dir: Path, agent_id: str) -> Path:
    return state_dir / f'agent-{agent_id}.heartbeat'


def touch_heartbeat(path: Path) -> None:
    """Create or update the heartbeat file's mtime to now."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    # `touch` only sets mtime if the file did not exist; force-refresh.
    now = datetime.now(UTC).timestamp()
    import os

    os.utime(path, (now, now))


def last_heartbeat(path: Path) -> datetime | None:
    """Return the heartbeat mtime as UTC datetime, or None if missing."""
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


def is_stale(path: Path, *, max_age_seconds: int = DEFAULT_STALE_AFTER_SECONDS) -> bool:
    """A worker is considered dead if its heartbeat is missing or too old.

    Missing-file counts as stale: it's safer to recover than to leave issues
    pinned to a worker that may never come back.
    """
    last = last_heartbeat(path)
    if last is None:
        return True
    age = (datetime.now(UTC) - last).total_seconds()
    return age > max_age_seconds
