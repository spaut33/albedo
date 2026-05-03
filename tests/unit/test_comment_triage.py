"""Tests for the LLM-based redispatch triage gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from albedo.claude_runner import ClaudeRunError, ClaudeRunResult
from albedo.comment_triage import triage_user_reply


def _result(
    text: str, *, is_error: bool = False, timed_out: bool = False
) -> ClaudeRunResult:
    return ClaudeRunResult(
        is_error=is_error,
        exit_code=0 if not is_error else 1,
        result_text=text,
        total_cost_usd=0.0,
        usage={},
        raw={},
        timed_out=timed_out,
    )


def _fake_spawn_returning(text: str, **flags: bool) -> tuple[Any, dict[str, Any]]:
    """Spawn stub that records the call kwargs and returns a canned result."""
    captured: dict[str, Any] = {}

    def spawn(**kwargs: Any) -> ClaudeRunResult:
        captured.update(kwargs)
        return _result(text, **flags)

    return spawn, captured


def test_yes_verdict_is_substantive(tmp_path: Path) -> None:
    spawn, _ = _fake_spawn_returning('VERDICT: YES -- gives concrete decision')
    verdict = triage_user_reply(
        agent_message='Should we use SQLite or Postgres?',
        user_replies=['SQLite is fine'],
        cwd=tmp_path,
        spawn=spawn,
    )
    assert verdict.is_substantive is True
    assert 'concrete decision' in verdict.reason


def test_no_verdict_is_not_substantive(tmp_path: Path) -> None:
    spawn, _ = _fake_spawn_returning('VERDICT: NO -- only deferring')
    verdict = triage_user_reply(
        agent_message='Should we use SQLite or Postgres?',
        user_replies=['надо подумать...'],
        cwd=tmp_path,
        spawn=spawn,
    )
    assert verdict.is_substantive is False
    assert 'deferring' in verdict.reason


def test_verdict_in_middle_of_output_is_picked_up(tmp_path: Path) -> None:
    """Models sometimes prefix the line — only the VERDICT token matters."""
    spawn, _ = _fake_spawn_returning(
        'thinking out loud: hmm\nVERDICT: NO — pure ack ("ok")\nthanks!'
    )
    verdict = triage_user_reply(
        agent_message='ready for review',
        user_replies=['ok'],
        cwd=tmp_path,
        spawn=spawn,
    )
    assert verdict.is_substantive is False


def test_unparseable_output_fails_open(tmp_path: Path) -> None:
    spawn, _ = _fake_spawn_returning('I cannot decide right now.')
    verdict = triage_user_reply(
        agent_message='q?',
        user_replies=['answer'],
        cwd=tmp_path,
        spawn=spawn,
    )
    assert verdict.is_substantive is True
    assert 'unparseable' in verdict.reason


def test_timeout_fails_open(tmp_path: Path) -> None:
    spawn, _ = _fake_spawn_returning('', timed_out=True)
    verdict = triage_user_reply(
        agent_message='q?',
        user_replies=['answer'],
        cwd=tmp_path,
        spawn=spawn,
    )
    assert verdict.is_substantive is True
    assert 'timed out' in verdict.reason


def test_claude_error_fails_open(tmp_path: Path) -> None:
    spawn, _ = _fake_spawn_returning('boom', is_error=True)
    verdict = triage_user_reply(
        agent_message='q?',
        user_replies=['answer'],
        cwd=tmp_path,
        spawn=spawn,
    )
    assert verdict.is_substantive is True


def test_run_error_fails_open(tmp_path: Path) -> None:
    def spawn(**_: Any) -> ClaudeRunResult:
        raise ClaudeRunError('cli not found')

    verdict = triage_user_reply(
        agent_message='q?',
        user_replies=['answer'],
        cwd=tmp_path,
        spawn=spawn,
    )
    assert verdict.is_substantive is True
    assert 'cli not found' in verdict.reason


def test_unexpected_exception_fails_open(tmp_path: Path) -> None:
    def spawn(**_: Any) -> ClaudeRunResult:
        raise RuntimeError('network')

    verdict = triage_user_reply(
        agent_message='q?',
        user_replies=['answer'],
        cwd=tmp_path,
        spawn=spawn,
    )
    assert verdict.is_substantive is True


def test_empty_user_replies_short_circuits(tmp_path: Path) -> None:
    """No replies → not substantive, no LLM call."""
    called = False

    def spawn(**_: Any) -> ClaudeRunResult:
        nonlocal called
        called = True
        return _result('VERDICT: YES')

    verdict = triage_user_reply(
        agent_message='q?',
        user_replies=[],
        cwd=tmp_path,
        spawn=spawn,
    )
    assert verdict.is_substantive is False
    assert called is False


def test_prompt_includes_agent_and_user_messages(tmp_path: Path) -> None:
    spawn, captured = _fake_spawn_returning('VERDICT: YES -- ok')
    triage_user_reply(
        agent_message='use which DB?',
        user_replies=['SQLite', 'actually Postgres'],
        cwd=tmp_path,
        spawn=spawn,
    )
    prompt = captured['prompt']
    assert isinstance(prompt, str)
    assert 'use which DB?' in prompt
    assert 'SQLite' in prompt
    assert 'actually Postgres' in prompt
    # No tools, no MCP, single-turn — these guarantees keep the call cheap.
    assert captured['allowed_tools'] == []
    assert captured['mcp_config_path'] is None
    assert captured['max_turns'] == 1


def test_spawn_receives_model_when_set(tmp_path: Path) -> None:
    spawn, captured = _fake_spawn_returning('VERDICT: YES -- ok')
    triage_user_reply(
        agent_message='q?',
        user_replies=['answer'],
        cwd=tmp_path,
        spawn=spawn,
        model='claude-haiku-4-5',
    )
    assert captured['model'] == 'claude-haiku-4-5'


def test_spawn_receives_none_model_by_default(tmp_path: Path) -> None:
    spawn, captured = _fake_spawn_returning('VERDICT: YES -- ok')
    triage_user_reply(
        agent_message='q?',
        user_replies=['answer'],
        cwd=tmp_path,
        spawn=spawn,
    )
    assert captured['model'] is None


def test_spawn_receives_cwd(tmp_path: Path) -> None:
    spawn, captured = _fake_spawn_returning('VERDICT: NO -- meh')
    triage_user_reply(
        agent_message='x',
        user_replies=['meh'],
        cwd=tmp_path,
        spawn=spawn,
    )
    assert captured['cwd'] == tmp_path
