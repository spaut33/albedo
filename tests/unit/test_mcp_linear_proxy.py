"""Unit tests for the Linear stdio MCP proxy."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from albedo.linear_client import Comment, Issue, LinearClient, LinearError
from albedo.mcp_audit import audit_log_path
from albedo.mcp_linear_proxy import (
    OUTCOME_BAD_ARGS,
    OUTCOME_ERROR,
    OUTCOME_OK,
    OUTCOME_SCOPE_DENIED,
    PROTOCOL_VERSION,
    LinearProxyHandler,
    ProxyContext,
    build_parser,
    handle_message,
    main,
    serve_stdio,
)

ISSUE_IDENTIFIER = 'AI-73'
ISSUE_UUID = 'uuid-bound-issue'
PROJECT_ID = 'project-uuid-aaa'
STATE_DIR_NAME = 'state'


def _make_context(state_dir: Path) -> ProxyContext:
    return ProxyContext(
        issue_identifier=ISSUE_IDENTIFIER,
        issue_uuid=ISSUE_UUID,
        agent_id='0',
        project_id=PROJECT_ID,
        state_dir=state_dir,
    )


def _make_issue() -> Issue:
    return Issue(
        id=ISSUE_UUID,
        identifier=ISSUE_IDENTIFIER,
        title='Stub',
        description='body',
        state_id='state-1',
        state_name='Backlog',
        assignee_id=None,
        label_ids=(),
        label_names=(),
        parent_id=None,
        branch_name='task/ai-73',
        url='https://linear.app/x/issue/AI-73',
    )


def _ticking_clock() -> Iterator[float]:
    """Deterministic monotonic clock: 0.0, 0.1, 0.2, ..."""
    n = 0
    while True:
        yield n * 0.1
        n += 1


def _read_audit_lines(state_dir: Path) -> list[dict[str, Any]]:
    path = audit_log_path(state_dir)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def test_in_scope_get_issue_calls_client_and_audits_ok(tmp_path: Path) -> None:
    state_dir = tmp_path / STATE_DIR_NAME
    client = MagicMock(spec=LinearClient)
    client.get_issue.return_value = _make_issue()
    clock_iter = _ticking_clock()
    handler = LinearProxyHandler(
        client, _make_context(state_dir), clock=lambda: next(clock_iter)
    )

    result = handler.call_tool('linear_get_issue', {'issue_id': ISSUE_IDENTIFIER})

    client.get_issue.assert_called_once_with(ISSUE_IDENTIFIER)
    assert result['isError'] is False
    payload = json.loads(result['content'][0]['text'])
    assert payload['identifier'] == ISSUE_IDENTIFIER

    audit = _read_audit_lines(state_dir)
    assert len(audit) == 1
    assert audit[0]['outcome'] == OUTCOME_OK
    assert audit[0]['tool'] == 'linear_get_issue'
    assert audit[0]['issue_id'] == ISSUE_IDENTIFIER
    assert audit[0]['agent_id'] == '0'


def test_uuid_form_also_in_scope(tmp_path: Path) -> None:
    state_dir = tmp_path / STATE_DIR_NAME
    client = MagicMock(spec=LinearClient)
    client.get_issue.return_value = _make_issue()
    handler = LinearProxyHandler(client, _make_context(state_dir))

    result = handler.call_tool('linear_get_issue', {'issue_id': ISSUE_UUID})

    client.get_issue.assert_called_once_with(ISSUE_UUID)
    assert result['isError'] is False


def test_out_of_scope_call_refused_without_outbound(tmp_path: Path) -> None:
    state_dir = tmp_path / STATE_DIR_NAME
    client = MagicMock(spec=LinearClient)
    handler = LinearProxyHandler(client, _make_context(state_dir))

    result = handler.call_tool(
        'linear_add_comment',
        {'issue_id': 'OTHER-99', 'body': 'should never post'},
    )

    # No outbound to Linear.
    client.add_comment.assert_not_called()
    client.get_issue.assert_not_called()
    client.list_comments.assert_not_called()

    # Structured error payload.
    assert result['isError'] is True
    structured = json.loads(result['content'][0]['text'])
    assert structured['error'] == 'scope_denied'
    assert structured['requested_issue_id'] == 'OTHER-99'
    assert structured['allowed_issue_identifier'] == ISSUE_IDENTIFIER
    assert structured['project_id'] == PROJECT_ID

    # And one audit entry recorded as scope_denied.
    audit = _read_audit_lines(state_dir)
    assert len(audit) == 1
    assert audit[0]['outcome'] == OUTCOME_SCOPE_DENIED
    assert audit[0]['tool'] == 'linear_add_comment'


def test_add_comment_redacts_body_in_audit(tmp_path: Path) -> None:
    state_dir = tmp_path / STATE_DIR_NAME
    client = MagicMock(spec=LinearClient)
    client.add_comment.return_value = 'comment-id-1'
    handler = LinearProxyHandler(client, _make_context(state_dir))

    long_body = 'x' * 1000
    result = handler.call_tool(
        'linear_add_comment',
        {'issue_id': ISSUE_UUID, 'body': long_body},
    )

    client.add_comment.assert_called_once_with(ISSUE_UUID, long_body)
    assert result['isError'] is False
    audit = _read_audit_lines(state_dir)
    # The raw 1000-char body must NOT appear in the audit summary.
    assert long_body not in audit[0]['args_summary']
    assert '<1000 chars>' in audit[0]['args_summary']


def test_list_comments_returns_serialized_dataclass(tmp_path: Path) -> None:
    state_dir = tmp_path / STATE_DIR_NAME
    client = MagicMock(spec=LinearClient)
    client.list_comments.return_value = [
        Comment(id='c1', body='hello', author_id='u1', created_at='2026-01-01'),
    ]
    handler = LinearProxyHandler(client, _make_context(state_dir))

    result = handler.call_tool('linear_list_comments', {'issue_id': ISSUE_IDENTIFIER})

    assert result['isError'] is False
    payload = json.loads(result['content'][0]['text'])
    assert payload == [
        {
            'id': 'c1',
            'body': 'hello',
            'author_id': 'u1',
            'created_at': '2026-01-01',
        }
    ]
    audit = _read_audit_lines(state_dir)
    assert len(audit) == 1
    assert audit[0]['outcome'] == OUTCOME_OK
    assert audit[0]['tool'] == 'linear_list_comments'


def test_linear_error_audited_as_outcome_error(tmp_path: Path) -> None:
    state_dir = tmp_path / STATE_DIR_NAME
    client = MagicMock(spec=LinearClient)
    client.get_issue.side_effect = LinearError('boom')
    handler = LinearProxyHandler(client, _make_context(state_dir))

    result = handler.call_tool('linear_get_issue', {'issue_id': ISSUE_IDENTIFIER})

    assert result['isError'] is True
    structured = json.loads(result['content'][0]['text'])
    assert 'boom' in structured['error']
    audit = _read_audit_lines(state_dir)
    assert len(audit) == 1
    assert audit[0]['outcome'] == OUTCOME_ERROR
    assert audit[0]['tool'] == 'linear_get_issue'


def test_missing_body_audited_as_bad_args(tmp_path: Path) -> None:
    state_dir = tmp_path / STATE_DIR_NAME
    client = MagicMock(spec=LinearClient)
    handler = LinearProxyHandler(client, _make_context(state_dir))

    result = handler.call_tool(
        'linear_add_comment',
        {'issue_id': ISSUE_IDENTIFIER, 'body': '   '},
    )

    assert result['isError'] is True
    client.add_comment.assert_not_called()
    audit = _read_audit_lines(state_dir)
    assert len(audit) == 1
    assert audit[0]['outcome'] == OUTCOME_BAD_ARGS


def test_unknown_tool_returns_error_and_audits_bad_args(tmp_path: Path) -> None:
    state_dir = tmp_path / STATE_DIR_NAME
    client = MagicMock(spec=LinearClient)
    handler = LinearProxyHandler(client, _make_context(state_dir))

    result = handler.call_tool('does_not_exist', {})

    assert result['isError'] is True
    audit = _read_audit_lines(state_dir)
    assert audit[0]['outcome'] == OUTCOME_BAD_ARGS


def test_missing_issue_id_audited_as_bad_args(tmp_path: Path) -> None:
    state_dir = tmp_path / STATE_DIR_NAME
    client = MagicMock(spec=LinearClient)
    handler = LinearProxyHandler(client, _make_context(state_dir))

    result = handler.call_tool('linear_get_issue', {})

    assert result['isError'] is True
    client.get_issue.assert_not_called()
    audit = _read_audit_lines(state_dir)
    assert audit[0]['outcome'] == OUTCOME_BAD_ARGS


def test_initialize_handshake_returns_protocol_and_capabilities(tmp_path: Path) -> None:
    state_dir = tmp_path / STATE_DIR_NAME
    client = MagicMock(spec=LinearClient)
    handler = LinearProxyHandler(client, _make_context(state_dir))

    response = handle_message(
        handler, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}
    )

    assert response is not None
    assert response['result']['protocolVersion'] == PROTOCOL_VERSION
    assert 'tools' in response['result']['capabilities']


def test_tools_list_returns_documented_inventory(tmp_path: Path) -> None:
    state_dir = tmp_path / STATE_DIR_NAME
    client = MagicMock(spec=LinearClient)
    handler = LinearProxyHandler(client, _make_context(state_dir))

    response = handle_message(
        handler, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}
    )

    assert response is not None
    names = {tool['name'] for tool in response['result']['tools']}
    assert names == {'linear_get_issue', 'linear_list_comments', 'linear_add_comment'}


def test_serve_stdio_round_trip(tmp_path: Path) -> None:
    state_dir = tmp_path / STATE_DIR_NAME
    client = MagicMock(spec=LinearClient)
    client.get_issue.return_value = _make_issue()
    handler = LinearProxyHandler(client, _make_context(state_dir))

    inp = io.StringIO(
        json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        + '\n'
        + json.dumps(
            {
                'jsonrpc': '2.0',
                'id': 2,
                'method': 'tools/call',
                'params': {
                    'name': 'linear_get_issue',
                    'arguments': {'issue_id': ISSUE_IDENTIFIER},
                },
            }
        )
        + '\n'
    )
    out = io.StringIO()
    serve_stdio(handler, input_stream=inp, output_stream=out)

    lines = [json.loads(line) for line in out.getvalue().splitlines() if line]
    assert len(lines) == 2
    assert lines[0]['id'] == 1
    assert {t['name'] for t in lines[0]['result']['tools']} == {
        'linear_get_issue',
        'linear_list_comments',
        'linear_add_comment',
    }
    assert lines[1]['id'] == 2
    assert lines[1]['result']['isError'] is False


def test_notifications_get_no_response(tmp_path: Path) -> None:
    state_dir = tmp_path / STATE_DIR_NAME
    client = MagicMock(spec=LinearClient)
    handler = LinearProxyHandler(client, _make_context(state_dir))

    response = handle_message(
        handler, {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
    )
    assert response is None


def test_main_exits_when_linear_api_key_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Patch the module's `load_linear_api_key` so neither process env nor the
    # operator's real `$ALBEDO_HOME/.env` can satisfy the lookup, regardless
    # of what they contain.
    def _raise_missing(*_: Any, **__: Any) -> Any:
        raise RuntimeError('LINEAR_API_KEY is not set. Put it in /tmp/.env')

    monkeypatch.setattr('albedo.mcp_linear_proxy.load_linear_api_key', _raise_missing)

    rc = main(
        [
            '--issue-identifier',
            ISSUE_IDENTIFIER,
            '--issue-uuid',
            ISSUE_UUID,
            '--agent-id',
            '0',
            '--project-id',
            PROJECT_ID,
            '--state-dir',
            str(tmp_path / 'state'),
        ]
    )

    assert rc == 2
    err = capsys.readouterr().err
    assert 'LINEAR_API_KEY' in err


def test_parser_accepts_no_flags_now_that_env_fallback_exists() -> None:
    """Required values may now arrive via ALBEDO_* env vars; the parser is
    intentionally permissive so `mcp-servers.json` can omit per-spawn args."""
    parser = build_parser()
    ns = parser.parse_args([])
    assert ns.issue_identifier is None
    assert ns.issue_uuid is None
    assert ns.agent_id is None
    assert ns.project_id is None
    assert ns.state_dir is None


def test_main_exits_with_diagnostic_when_required_args_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No CLI args, no ALBEDO_* env → main exits 2 with a single diagnostic."""
    for var in (
        'ALBEDO_ISSUE_IDENTIFIER',
        'ALBEDO_ISSUE_UUID',
        'ALBEDO_AGENT_ID',
        'ALBEDO_PROJECT_ID',
        'ALBEDO_STATE_DIR',
    ):
        monkeypatch.delenv(var, raising=False)

    rc = main([])

    assert rc == 2
    err = capsys.readouterr().err
    assert '--issue-identifier' in err
    assert 'ALBEDO_ISSUE_IDENTIFIER' in err
