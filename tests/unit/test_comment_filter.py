"""Tests for human-vs-bot comment filtering."""

from __future__ import annotations

from albedo.comment_filter import (
    filter_user_comments,
    format_user_comments_block,
)
from albedo.linear_client import Comment


def _c(comment_id: str, body: str, author_id: str | None = 'human-1') -> Comment:
    return Comment(id=comment_id, body=body, author_id=author_id)


def test_filter_drops_known_bot_user_ids() -> None:
    comments = [
        _c('1', 'human says hi', 'human-1'),
        _c('2', 'bot reply', 'bot-7'),
        _c('3', 'another human', 'human-2'),
    ]
    filtered = filter_user_comments(comments, frozenset({'bot-7'}))
    assert [c.id for c in filtered] == ['1', '3']


def test_filter_drops_agent_prefix_regardless_of_author() -> None:
    comments = [
        _c('1', '**agent-1**: BLOCKED: missing spec', 'human-1'),
        _c('2', '**agent-2**: PR: https://github.com/x/y/pull/3', 'unknown'),
        _c('3', '  **agent-12**:VERDICT: APPROVE', 'shared-bot'),
        _c('4', '**housekeeping**: Decomposition approved — released AI-7', 'shared'),
        _c('5', 'real user comment', 'human-1'),
    ]
    filtered = filter_user_comments(comments, frozenset())
    assert [c.id for c in filtered] == ['5']


def test_filter_preserves_input_order() -> None:
    comments = [_c(str(i), f'msg {i}', 'human-1') for i in range(5)]
    filtered = filter_user_comments(comments, frozenset())
    assert [c.id for c in filtered] == ['0', '1', '2', '3', '4']


def test_filter_empty_input() -> None:
    assert filter_user_comments([], frozenset({'bot-7'})) == []


def test_filter_keeps_comment_with_no_author() -> None:
    comments = [_c('1', 'genuine text', None)]
    filtered = filter_user_comments(comments, frozenset({'bot-7'}))
    assert filtered == comments


def test_format_block_renders_bullets_with_indented_body() -> None:
    block = format_user_comments_block(
        [
            _c('1', 'first line\nsecond line', 'human-1'),
            _c('2', 'short', 'human-2'),
        ]
    )
    assert '- (author=human-1)' in block
    assert '      first line' in block
    assert '      second line' in block
    assert '- (author=human-2)' in block
    assert '      short' in block


def test_format_block_skips_empty_bodies() -> None:
    block = format_user_comments_block(
        [
            _c('1', '   ', 'human-1'),
            _c('2', 'real', 'human-2'),
        ]
    )
    assert 'human-1' not in block
    assert 'human-2' in block


def test_format_block_returns_empty_for_no_comments() -> None:
    assert format_user_comments_block([]) == ''
