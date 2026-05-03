"""Tests for the stats summary helpers used by the TUI header."""

from __future__ import annotations

import time
from pathlib import Path

from albedo.stats import (
    StatsContext,
    daily_cost_usd,
    daily_summary,
    session_cost_usd,
    session_summary,
)
from albedo.usage import UsageLedger


def _make_ledger(tmp_path: Path) -> UsageLedger:
    return UsageLedger(tmp_path / 'usage.db')


def test_session_summary_counts_done_and_failed(tmp_path: Path) -> None:
    ledger = _make_ledger(tmp_path)
    now = int(time.time())
    ledger.record_usage(
        agent_id='1',
        issue_id='LIN-1',
        usage={'input_tokens': 10, 'output_tokens': 5},
        outcome='done',
        cost_usd=0.5,
        ts_unix=now - 1,
    )
    ledger.record_usage(
        agent_id='2',
        issue_id='LIN-2',
        usage={'input_tokens': 1, 'output_tokens': 1},
        outcome='fail',
        cost_usd=0.1,
        ts_unix=now - 2,
    )
    ctx = StatsContext(
        ledger=ledger, session_started_at_unix=now - 60, cache_seconds=0.0
    )
    line = session_summary(ctx, now_unix=now)
    assert 'done 1' in line
    assert 'fail 1' in line
    assert '$' not in line  # cost moved to tokens panel
    assert abs(session_cost_usd(ctx, now_unix=now) - 0.6) < 1e-6


def test_daily_summary_window(tmp_path: Path) -> None:
    ledger = _make_ledger(tmp_path)
    now = int(time.time())
    # Inside window
    ledger.record_usage(
        agent_id='1',
        issue_id='LIN-1',
        usage={'input_tokens': 0, 'output_tokens': 0},
        outcome='done',
        cost_usd=1.25,
        ts_unix=now - 60,
    )
    # Outside window
    ledger.record_usage(
        agent_id='1',
        issue_id='LIN-old',
        usage={'input_tokens': 0, 'output_tokens': 0},
        outcome='done',
        cost_usd=99.0,
        ts_unix=now - 60 * 60 * 25,  # 25h ago
    )
    ctx = StatsContext(
        ledger=ledger, session_started_at_unix=now - 60, cache_seconds=0.0
    )
    line = daily_summary(ctx, now_unix=now)
    assert 'done 1' in line
    assert '$' not in line  # cost moved to tokens panel
    assert abs(daily_cost_usd(ctx, now_unix=now) - 1.25) < 1e-6


def test_caching_avoids_repeat_queries(tmp_path: Path) -> None:
    ledger = _make_ledger(tmp_path)
    now = int(time.time())
    ledger.record_usage(
        agent_id='1',
        issue_id='LIN-1',
        usage={'input_tokens': 0, 'output_tokens': 0},
        outcome='done',
        cost_usd=0.5,
        ts_unix=now - 1,
    )
    ctx = StatsContext(
        ledger=ledger, session_started_at_unix=now - 60, cache_seconds=10.0
    )
    first = session_summary(ctx, now_unix=now)
    # New record should NOT show until cache expires.
    ledger.record_usage(
        agent_id='1',
        issue_id='LIN-2',
        usage={'input_tokens': 0, 'output_tokens': 0},
        outcome='done',
        cost_usd=10.0,
        ts_unix=now - 1,
    )
    second = session_summary(ctx, now_unix=now)
    assert first == second
