"""Tests for the file-based heartbeat."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from albedo.heartbeat import (
    DEFAULT_STALE_AFTER_SECONDS,
    heartbeat_path,
    is_stale,
    last_heartbeat,
    touch_heartbeat,
)


def test_heartbeat_path_assembles() -> None:
    assert heartbeat_path(Path('/tmp/state'), '1') == Path(
        '/tmp/state/agent-1.heartbeat'
    )


def test_touch_creates_file_and_directory(tmp_path: Path) -> None:
    state = tmp_path / 'state' / 'nested'
    path = heartbeat_path(state, '1')
    touch_heartbeat(path)
    assert path.exists()


def test_last_heartbeat_returns_utc_datetime(tmp_path: Path) -> None:
    path = tmp_path / 'hb'
    touch_heartbeat(path)
    last = last_heartbeat(path)
    assert last is not None
    assert last.tzinfo == UTC


def test_last_heartbeat_returns_none_when_missing(tmp_path: Path) -> None:
    assert last_heartbeat(tmp_path / 'missing') is None


def test_is_stale_when_missing(tmp_path: Path) -> None:
    assert is_stale(tmp_path / 'missing') is True


def test_is_stale_after_threshold(tmp_path: Path) -> None:
    path = tmp_path / 'hb'
    touch_heartbeat(path)
    long_ago = (
        datetime.now(UTC) - timedelta(seconds=DEFAULT_STALE_AFTER_SECONDS + 60)
    ).timestamp()
    os.utime(path, (long_ago, long_ago))
    assert is_stale(path) is True


def test_is_fresh_when_just_touched(tmp_path: Path) -> None:
    path = tmp_path / 'hb'
    touch_heartbeat(path)
    assert is_stale(path) is False


def test_is_stale_respects_max_age(tmp_path: Path) -> None:
    path = tmp_path / 'hb'
    touch_heartbeat(path)
    one_minute_ago = (datetime.now(UTC) - timedelta(seconds=70)).timestamp()
    os.utime(path, (one_minute_ago, one_minute_ago))
    assert is_stale(path, max_age_seconds=60) is True
    assert is_stale(path, max_age_seconds=120) is False
