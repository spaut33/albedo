"""Tests for `usage_limit` parsing and pause persistence."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from albedo.usage_limit import (
    PauseState,
    ResetInfo,
    clear_pause_state,
    format_pause_summary,
    is_paused,
    parse_reset_time,
    pause_state_path,
    read_pause_state,
    record_pause,
    write_pause_state,
)


def test_parse_reset_time_extracts_pm_time_in_named_tz() -> None:
    msk = ZoneInfo('Europe/Moscow')
    now = datetime(2026, 5, 4, 17, 0, tzinfo=msk)
    info = parse_reset_time(
        "You've hit your limit · resets 7:40pm (Europe/Moscow)",
        now=now,
    )
    assert info is not None
    expected = datetime(2026, 5, 4, 19, 40, tzinfo=msk).timestamp()
    assert info.until_unix == expected
    assert info.tz_label == 'Europe/Moscow'


def test_parse_reset_time_handles_24h_form_and_curly_apostrophe() -> None:
    msk = ZoneInfo('Europe/Moscow')
    now = datetime(2026, 5, 4, 17, 0, tzinfo=msk)
    info = parse_reset_time(
        'You’ve hit your usage limit — resets at 19:40 (MSK)',  # noqa: RUF001
        now=now,
    )
    assert info is not None
    expected = datetime(2026, 5, 4, 19, 40, tzinfo=msk).timestamp()
    assert info.until_unix == expected
    assert info.tz_label == 'MSK'


def test_parse_reset_time_rolls_to_next_day_when_already_past() -> None:
    msk = ZoneInfo('Europe/Moscow')
    # "now" is past the announced reset → window must be tomorrow.
    now = datetime(2026, 5, 4, 22, 0, tzinfo=msk)
    info = parse_reset_time(
        "You've hit your limit · resets 7:40pm (Europe/Moscow)",
        now=now,
    )
    assert info is not None
    expected = datetime(2026, 5, 5, 19, 40, tzinfo=msk).timestamp()
    assert info.until_unix == expected


def test_parse_reset_time_returns_none_for_unknown_tz() -> None:
    info = parse_reset_time("You've hit your limit · resets 7pm (Mars/Olympus)")
    assert info is None


def test_parse_reset_time_returns_none_when_no_match() -> None:
    assert parse_reset_time('') is None
    assert parse_reset_time('totally unrelated error') is None


def test_parse_reset_time_handles_12pm_and_12am_correctly() -> None:
    utc = ZoneInfo('UTC')
    now = datetime(2026, 5, 4, 6, 0, tzinfo=utc)
    noon = parse_reset_time("You've hit your limit · resets 12pm (UTC)", now=now)
    assert noon is not None
    assert datetime.fromtimestamp(noon.until_unix, tz=utc).hour == 12

    midnight = parse_reset_time("You've hit your limit · resets 12am (UTC)", now=now)
    assert midnight is not None
    # 12am → next midnight (00:00 of the next day).
    midnight_dt = datetime.fromtimestamp(midnight.until_unix, tz=utc)
    assert midnight_dt.hour == 0
    assert midnight_dt.day == 5


def test_record_pause_writes_atomically_and_is_idempotent(tmp_path: Path) -> None:
    info = ResetInfo(until_unix=1_900_000_000.0, tz_label='UTC', raw_match='x')
    state = record_pause(
        state_dir=tmp_path,
        info=info,
        reason='claude usage limit',
        now_unix=1_800_000_000.0,
        safety_buffer_seconds=60,
    )
    assert state.until_unix == 1_900_000_060.0
    assert state.reason == 'claude usage limit'

    # Concurrent writer with the same window: no overwrite, returns the
    # already-set state.
    again = record_pause(
        state_dir=tmp_path,
        info=info,
        reason='different reason',
        now_unix=1_800_000_005.0,
    )
    assert again.until_unix == state.until_unix
    assert again.reason == state.reason  # original wins


def test_record_pause_extends_when_new_window_is_longer(tmp_path: Path) -> None:
    early = ResetInfo(until_unix=1_900_000_000.0, tz_label='UTC', raw_match='x')
    record_pause(
        state_dir=tmp_path,
        info=early,
        reason='first',
        now_unix=1_800_000_000.0,
        safety_buffer_seconds=0,
    )
    later = ResetInfo(until_unix=2_000_000_000.0, tz_label='UTC', raw_match='y')
    extended = record_pause(
        state_dir=tmp_path,
        info=later,
        reason='second',
        now_unix=1_800_000_010.0,
        safety_buffer_seconds=0,
    )
    assert extended.until_unix == 2_000_000_000.0
    assert extended.reason == 'second'


def test_is_paused_returns_state_until_window_elapses(tmp_path: Path) -> None:
    state = PauseState(
        until_unix=2_000_000_000.0,
        tz_label='UTC',
        reason='claude usage limit',
        set_at_unix=1_999_999_000.0,
    )
    write_pause_state(pause_state_path(tmp_path), state)

    active = is_paused(tmp_path, now_unix=1_999_999_500.0)
    assert active is not None
    assert active.until_unix == state.until_unix

    # After the window: caller sees None and the file is removed.
    assert is_paused(tmp_path, now_unix=2_000_000_001.0) is None
    assert not pause_state_path(tmp_path).exists()


def test_is_paused_handles_missing_file(tmp_path: Path) -> None:
    assert is_paused(tmp_path, now_unix=0.0) is None


def test_read_pause_state_recovers_from_malformed_file(tmp_path: Path) -> None:
    path = pause_state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('not json', encoding='utf-8')
    assert read_pause_state(path) is None


def test_format_pause_summary_renders_local_time_in_tz() -> None:
    state = PauseState(
        until_unix=datetime(
            2026, 5, 4, 19, 40, tzinfo=ZoneInfo('Europe/Moscow')
        ).timestamp(),
        tz_label='Europe/Moscow',
        reason='claude usage limit',
        set_at_unix=0.0,
    )
    assert (
        format_pause_summary(state)
        == 'paused until 19:40 Europe/Moscow — claude usage limit'
    )


def test_clear_pause_state_is_safe_when_missing(tmp_path: Path) -> None:
    # Should not raise even though the file was never written.
    clear_pause_state(pause_state_path(tmp_path))


def test_write_pause_state_round_trips(tmp_path: Path) -> None:
    state = PauseState(
        until_unix=12345.0,
        tz_label='UTC',
        reason='r',
        set_at_unix=1.0,
    )
    path = pause_state_path(tmp_path)
    write_pause_state(path, state)
    payload = json.loads(path.read_text(encoding='utf-8'))
    assert payload['until_unix'] == 12345.0
    assert payload['tz_label'] == 'UTC'
    assert payload['reason'] == 'r'
    assert payload['set_at_unix'] == 1.0
