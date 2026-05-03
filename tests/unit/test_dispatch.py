"""Tests for column → role dispatch."""

from __future__ import annotations

import pytest

from albedo.dispatch import (
    ARCHITECT,
    CODER,
    REVIEWER,
    UnknownColumnError,
    dispatch,
)


def test_dispatch_returns_coder_for_backlog() -> None:
    assert dispatch('Backlog') is CODER


def test_dispatch_raises_on_unknown_state() -> None:
    with pytest.raises(UnknownColumnError, match='Awaiting approval'):
        dispatch('Awaiting approval')


def test_coder_spec_has_expected_targets() -> None:
    assert CODER.target_state_on_success == 'Review'
    assert CODER.target_state_on_blocker == 'Backlog'
    assert CODER.timeout_minutes == 30
    assert CODER.max_turns == 50
    assert 'Edit' in CODER.allowed_tools


def test_architect_runs_under_plan_mode() -> None:
    assert ARCHITECT.permission_mode == 'plan'


def test_other_roles_have_no_permission_mode() -> None:
    assert CODER.permission_mode is None
    assert REVIEWER.permission_mode is None
