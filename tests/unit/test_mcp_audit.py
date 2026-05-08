"""Unit tests for the shared MCP audit logger."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from albedo.mcp_audit import (
    ARGS_SUMMARY_MAX_CHARS,
    audit_log_path,
    record_audit,
    summarize_args,
)

_REQUIRED_FIELDS = (
    'ts',
    'issue_id',
    'agent_id',
    'tool',
    'args_summary',
    'outcome',
    'latency_ms',
)


def test_record_audit_appends_one_parseable_json_line(tmp_path: Path) -> None:
    record_audit(
        state_dir=tmp_path,
        issue_id='AI-71',
        agent_id='3',
        tool='github.create_pull_request',
        args_summary='title=feat: ...',
        outcome='ok',
        latency_ms=42,
    )
    lines = audit_log_path(tmp_path).read_text(encoding='utf-8').splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    for field in _REQUIRED_FIELDS:
        assert field in entry, f'missing {field}'
    assert entry['issue_id'] == 'AI-71'
    assert entry['agent_id'] == '3'
    assert entry['tool'] == 'github.create_pull_request'
    assert entry['args_summary'] == 'title=feat: ...'
    assert entry['outcome'] == 'ok'
    assert isinstance(entry['latency_ms'], int)
    assert entry['latency_ms'] >= 0


def test_record_audit_lands_under_logs_next_to_process_logs(tmp_path: Path) -> None:
    record_audit(
        state_dir=tmp_path,
        issue_id='AI-71',
        agent_id='1',
        tool='t',
        args_summary='',
        outcome='ok',
        latency_ms=0,
    )
    assert audit_log_path(tmp_path) == tmp_path / 'logs' / 'mcp-audit.jsonl'
    assert audit_log_path(tmp_path).exists()


def test_record_audit_each_call_writes_one_line(tmp_path: Path) -> None:
    for i in range(5):
        record_audit(
            state_dir=tmp_path,
            issue_id='AI-71',
            agent_id='3',
            tool=f'tool-{i}',
            args_summary='ok',
            outcome='ok',
            latency_ms=i,
        )
    lines = audit_log_path(tmp_path).read_text(encoding='utf-8').splitlines()
    assert len(lines) == 5
    for i, line in enumerate(lines):
        entry = json.loads(line)
        assert entry['tool'] == f'tool-{i}'
        assert entry['latency_ms'] == i


def test_record_audit_truncates_args_summary(tmp_path: Path) -> None:
    record_audit(
        state_dir=tmp_path,
        issue_id='AI-71',
        agent_id='3',
        tool='t',
        args_summary='a' * 1000,
        outcome='ok',
        latency_ms=1,
    )
    entry = json.loads(audit_log_path(tmp_path).read_text(encoding='utf-8'))
    assert len(entry['args_summary']) <= ARGS_SUMMARY_MAX_CHARS


def test_record_audit_clamps_negative_latency(tmp_path: Path) -> None:
    record_audit(
        state_dir=tmp_path,
        issue_id='AI-71',
        agent_id='3',
        tool='t',
        args_summary='',
        outcome='ok',
        latency_ms=-5,
    )
    entry = json.loads(audit_log_path(tmp_path).read_text(encoding='utf-8'))
    assert entry['latency_ms'] == 0


def test_record_audit_swallows_unwritable_dir(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Create a regular file at `<state_dir>/logs` so `mkdir(parents=True,
    # exist_ok=True)` and the subsequent `open(..., 'a')` both raise
    # OSError. The function must log at WARNING and return cleanly.
    (tmp_path / 'logs').write_text('blocking', encoding='utf-8')
    with caplog.at_level(logging.WARNING, logger='albedo.mcp_audit'):
        record_audit(
            state_dir=tmp_path,
            issue_id='AI-71',
            agent_id='3',
            tool='t',
            args_summary='',
            outcome='ok',
            latency_ms=1,
        )
    assert any('mcp-audit' in record.message for record in caplog.records)


def test_summarize_args_truncates_long_payload() -> None:
    out = summarize_args({'k': 'v' * 1000})
    assert len(out) <= ARGS_SUMMARY_MAX_CHARS


def test_summarize_args_short_passthrough() -> None:
    out = summarize_args({'pr': 12, 'title': 'hello'})
    assert 'hello' in out
    assert len(out) <= ARGS_SUMMARY_MAX_CHARS


def test_summarize_args_handles_non_serializable() -> None:
    out = summarize_args({'p': Path('/tmp/x')})
    assert '/tmp/x' in out
