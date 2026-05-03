"""Tests for the SQLite-backed usage ledger."""

from __future__ import annotations

from pathlib import Path

import pytest

from albedo.usage import UsageLedger, default_db_path


def _ledger(tmp_path: Path) -> UsageLedger:
    return UsageLedger(default_db_path(tmp_path))


def test_default_db_path_assembles() -> None:
    assert default_db_path(Path('/tmp/state')) == Path('/tmp/state/usage.db')


def test_record_and_sum_window(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record_usage(
        agent_id='1',
        issue_id='AI-5',
        usage={
            'input_tokens': 100,
            'output_tokens': 50,
            'cache_creation_input_tokens': 1000,
            'cache_read_input_tokens': 5000,
        },
        ts_unix=1000,
    )
    ledger.record_usage(
        agent_id='2',
        issue_id='AI-6',
        usage={'input_tokens': 200, 'output_tokens': 75},
        ts_unix=1500,
    )
    # Within window: both records → 100+50+200+75 = 425. Cache tokens excluded.
    total = 100 + 50 + 200 + 75
    assert ledger.tokens_in_window(window_seconds=600, now_unix=1500) == total


def test_window_excludes_records_outside_horizon(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record_usage(
        agent_id='1',
        issue_id='AI-5',
        usage={'input_tokens': 100, 'output_tokens': 0},
        ts_unix=1000,
    )
    ledger.record_usage(
        agent_id='1',
        issue_id='AI-6',
        usage={'input_tokens': 200, 'output_tokens': 0},
        ts_unix=2000,
    )
    # Window of 100s ending at 1500 includes only the first record.
    assert ledger.tokens_in_window(window_seconds=100, now_unix=1050) == 100
    # Window of 1500s ending at 2100 includes both.
    assert ledger.tokens_in_window(window_seconds=1500, now_unix=2100) == 300


def test_should_throttle_compares_against_cap(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record_usage(
        agent_id='1',
        issue_id='AI-5',
        usage={'input_tokens': 200, 'output_tokens': 100},
        ts_unix=1000,
    )
    assert (
        ledger.should_throttle(token_cap=400, window_seconds=600, now_unix=1500)
        is False
    )
    assert (
        ledger.should_throttle(token_cap=300, window_seconds=600, now_unix=1500) is True
    )
    assert (
        ledger.should_throttle(token_cap=200, window_seconds=600, now_unix=1500) is True
    )


def test_should_throttle_disabled_when_cap_nonpositive(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record_usage(
        agent_id='1',
        issue_id='AI-5',
        usage={'input_tokens': 999_999, 'output_tokens': 0},
        ts_unix=1000,
    )
    assert (
        ledger.should_throttle(token_cap=0, window_seconds=600, now_unix=1500) is False
    )
    assert (
        ledger.should_throttle(token_cap=-1, window_seconds=600, now_unix=1500) is False
    )


def test_record_uses_now_when_ts_omitted(tmp_path: Path) -> None:
    import time

    ledger = _ledger(tmp_path)
    before = int(time.time())
    ledger.record_usage(
        agent_id='1',
        issue_id='AI-5',
        usage={'input_tokens': 1, 'output_tokens': 1},
    )
    after = int(time.time())
    # Window covering [before .. after] should include the row.
    assert ledger.tokens_in_window(window_seconds=after - before + 1) >= 2


def test_concurrent_writes_dont_corrupt(tmp_path: Path) -> None:
    """Smoke test: many record_usage calls don't lose rows."""
    import threading

    ledger = _ledger(tmp_path)

    def writer(start_idx: int) -> None:
        for i in range(20):
            ledger.record_usage(
                agent_id=f'{start_idx}',
                issue_id=f'AI-{start_idx}-{i}',
                usage={'input_tokens': 1, 'output_tokens': 1},
                ts_unix=10_000 + i,
            )

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 4 threads, 20 rows each, 2 tokens each.
    assert ledger.tokens_in_window(window_seconds=10_000, now_unix=20_000) == 4 * 20 * 2


def test_init_is_idempotent(tmp_path: Path) -> None:
    """Re-opening the same DB does not destroy existing rows."""
    ledger1 = _ledger(tmp_path)
    ledger1.record_usage(
        agent_id='1',
        issue_id='AI-5',
        usage={'input_tokens': 50, 'output_tokens': 50},
        ts_unix=1000,
    )
    ledger2 = UsageLedger(default_db_path(tmp_path))
    assert ledger2.tokens_in_window(window_seconds=600, now_unix=1500) == 100


@pytest.mark.parametrize('cap', [0, 100, 1_000_000])
def test_should_throttle_thresholds(tmp_path: Path, cap: int) -> None:
    """Round-trip through the API at different cap values."""
    ledger = _ledger(tmp_path)
    ledger.record_usage(
        agent_id='1',
        issue_id='AI-5',
        usage={'input_tokens': 500, 'output_tokens': 0},
        ts_unix=1000,
    )
    expected = (cap > 0) and (cap <= 500)
    assert (
        ledger.should_throttle(token_cap=cap, window_seconds=600, now_unix=1500)
        is expected
    )
