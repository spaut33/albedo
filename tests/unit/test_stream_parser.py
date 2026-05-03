"""Unit tests for the stream-json event parser."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from albedo.stream_parser import (
    RECENT_TOOL_BUFFER,
    THINKING_PREVIEW_CHARS,
    StreamSnapshot,
)


def _assistant(
    content: list[Mapping[str, Any]],
    usage: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {'content': content}
    if usage is not None:
        msg['usage'] = dict(usage)
    return {'type': 'assistant', 'message': msg}


def test_assistant_event_increments_turns() -> None:
    snap = StreamSnapshot()
    snap.feed(_assistant([{'type': 'text', 'text': 'hi'}]))
    snap.feed(_assistant([{'type': 'text', 'text': 'again'}]))
    assert snap.turns == 2


def test_tool_use_records_name_target_and_recent_buffer() -> None:
    snap = StreamSnapshot()
    snap.feed(
        _assistant(
            [
                {
                    'type': 'tool_use',
                    'name': 'Edit',
                    'input': {'file_path': 'src/foo.py'},
                }
            ]
        )
    )
    assert snap.tool_use_count == 1
    assert snap.last_tool is not None
    assert snap.last_tool.name == 'Edit'
    assert snap.last_tool.target == 'src/foo.py'
    assert len(snap.recent_tools) == 1


def test_tool_use_bash_summarizes_command() -> None:
    snap = StreamSnapshot()
    snap.feed(
        _assistant(
            [
                {
                    'type': 'tool_use',
                    'name': 'Bash',
                    'input': {'command': 'make test\nls'},
                }
            ]
        )
    )
    assert snap.last_tool is not None
    assert 'make test' in snap.last_tool.target
    assert '\n' not in snap.last_tool.target


def test_recent_tools_capped_at_buffer_size() -> None:
    snap = StreamSnapshot()
    for i in range(RECENT_TOOL_BUFFER + 3):
        snap.feed(
            _assistant(
                [
                    {
                        'type': 'tool_use',
                        'name': 'Read',
                        'input': {'file_path': f'f{i}.py'},
                    }
                ]
            )
        )
    assert len(snap.recent_tools) == RECENT_TOOL_BUFFER
    # Ring buffer keeps the most recent entries.
    assert snap.recent_tools[-1].target == f'f{RECENT_TOOL_BUFFER + 2}.py'


def test_text_event_truncates_thinking_preview() -> None:
    snap = StreamSnapshot()
    long_text = 'a' * (THINKING_PREVIEW_CHARS + 50)
    snap.feed(_assistant([{'type': 'text', 'text': long_text}]))
    assert len(snap.thinking_preview) == THINKING_PREVIEW_CHARS


def test_text_event_skips_empty() -> None:
    snap = StreamSnapshot()
    snap.feed(_assistant([{'type': 'text', 'text': '   '}]))
    assert snap.thinking_preview == ''


def test_assistant_usage_updates_token_counters() -> None:
    snap = StreamSnapshot()
    snap.feed(
        _assistant(
            [{'type': 'text', 'text': 'x'}],
            usage={
                'input_tokens': 100,
                'output_tokens': 20,
                'cache_read_input_tokens': 500,
                'cache_creation_input_tokens': 50,
            },
        )
    )
    assert snap.input_tokens == 100
    assert snap.output_tokens == 20
    assert snap.cache_read_tokens == 500
    assert snap.cache_creation_tokens == 50


def test_result_event_marks_finished_and_captures_summary() -> None:
    snap = StreamSnapshot()
    snap.feed(
        {
            'type': 'result',
            'is_error': False,
            'result': 'done',
            'total_cost_usd': 0.42,
            'usage': {'input_tokens': 11, 'output_tokens': 9},
        }
    )
    assert snap.finished is True
    assert snap.is_error is False
    assert snap.final_result_text == 'done'
    assert snap.total_cost_usd == 0.42
    assert snap.input_tokens == 11
    assert snap.final_event is not None


def test_unknown_event_type_is_ignored() -> None:
    snap = StreamSnapshot()
    snap.feed({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed'}})
    assert snap.turns == 0
    assert snap.tool_use_count == 0
    assert snap.finished is False


def test_mcp_tool_summary_lists_keys() -> None:
    snap = StreamSnapshot()
    snap.feed(
        _assistant(
            [
                {
                    'type': 'tool_use',
                    'name': 'mcp__linear-server__save_comment',
                    'input': {'issueId': 'abc', 'body': 'hi'},
                }
            ]
        )
    )
    assert snap.last_tool is not None
    target = snap.last_tool.target
    assert 'issueId' in target or 'body' in target


def test_grep_tool_uses_pattern() -> None:
    snap = StreamSnapshot()
    snap.feed(
        _assistant(
            [
                {
                    'type': 'tool_use',
                    'name': 'Grep',
                    'input': {'pattern': 'def foo'},
                }
            ]
        )
    )
    assert snap.last_tool is not None
    assert snap.last_tool.target == 'def foo'


def test_long_target_is_truncated_with_ellipsis() -> None:
    snap = StreamSnapshot()
    snap.feed(
        _assistant(
            [
                {
                    'type': 'tool_use',
                    'name': 'Bash',
                    'input': {'command': 'x' * 200},
                }
            ]
        )
    )
    assert snap.last_tool is not None
    assert snap.last_tool.target.endswith('…')
