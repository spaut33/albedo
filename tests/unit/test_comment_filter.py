"""Tests for human-vs-bot comment filtering."""

from __future__ import annotations

from albedo.comment_filter import (
    filter_user_comments,
    format_reviewer_feedback_block,
    format_user_comments_block,
    latest_reviewer_feedback,
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


def test_latest_reviewer_feedback_picks_newest_matching_bot_comment() -> None:
    comments = [
        _c('1', '**agent-1**: REVIEW REQUEST_CHANGES (attempts=1/5).\n\nold', 'bot'),
        _c('2', 'human asking a question', 'human-1'),
        _c('3', '**agent-2**: REVIEW REQUEST_CHANGES (attempts=2/5).\n\nnew', 'bot'),
        _c('4', 'another human reply', 'human-1'),
    ]
    latest = latest_reviewer_feedback(comments, frozenset({'bot'}))
    assert latest is not None
    assert latest.id == '3'


def test_latest_reviewer_feedback_accepts_approve_and_blocked() -> None:
    approve = _c('a', '**agent-1**: REVIEW APPROVE\n\nlooks good', 'bot')
    blocked = _c('b', '**agent-2**: BLOCKED: no PR url found.\n\n```\n...\n```', 'bot')
    assert latest_reviewer_feedback([approve], frozenset()) is approve
    assert latest_reviewer_feedback([blocked], frozenset()) is blocked


def test_latest_reviewer_feedback_ignores_user_comments() -> None:
    comments = [
        _c('1', '**agent-1**: REVIEW REQUEST_CHANGES.\n\ndetails', 'bot'),
        _c('2', 'REVIEW REQUEST_CHANGES — fake from a human', 'human-1'),
    ]
    latest = latest_reviewer_feedback(comments, frozenset({'bot'}))
    assert latest is not None
    assert latest.id == '1'


def test_latest_reviewer_feedback_ignores_non_verdict_bot_comments() -> None:
    comments = [
        _c('1', '**agent-1**: PR: https://github.com/x/y/pull/3', 'bot'),
        _c('2', '**housekeeping**: Decomposition approved', 'bot'),
    ]
    assert latest_reviewer_feedback(comments, frozenset({'bot'})) is None


def test_latest_reviewer_feedback_empty_list() -> None:
    assert latest_reviewer_feedback([], frozenset()) is None


def test_format_reviewer_feedback_block_quotes_body() -> None:
    comment = _c(
        '1',
        '**agent-2**: REVIEW REQUEST_CHANGES (attempts=1/5).\n\nfix the regex',
        'bot',
    )
    block = format_reviewer_feedback_block(comment)
    assert block.startswith('> **agent-2**: REVIEW REQUEST_CHANGES')
    assert '> fix the regex' in block


def test_format_reviewer_feedback_block_handles_none_and_empty() -> None:
    assert format_reviewer_feedback_block(None) == ''
    assert format_reviewer_feedback_block(_c('1', '   ', 'bot')) == ''
