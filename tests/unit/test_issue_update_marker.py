"""Tests for the `<<<ISSUE_UPDATE>>>` agent marker (Part C)."""

from __future__ import annotations

from albedo.claude_runner import ClaudeRunResult
from albedo.linear_client import Issue, IssueUpdate
from albedo.worker import (
    apply_issue_update_marker,
    parse_decomposition,
    parse_issue_update,
)


def _issue(description: str = 'old body') -> Issue:
    return Issue(
        id='uuid-1',
        identifier='AI-5',
        title='Add filter',
        description=description,
        state_id='s1',
        state_name='Backlog',
        assignee_id=None,
        label_ids=(),
        label_names=(),
        parent_id=None,
        branch_name='',
    )


class _FakeLinear:
    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []
        self.updates: list[tuple[str, IssueUpdate]] = []

    def add_comment(self, issue_id: str, body: str) -> str:
        self.comments.append((issue_id, body))
        return 'c'

    def update_issue(self, issue_id: str, update: IssueUpdate) -> Issue:
        self.updates.append((issue_id, update))
        return _issue()


def _result(text: str) -> ClaudeRunResult:
    return ClaudeRunResult(
        is_error=False,
        exit_code=0,
        result_text=text,
        total_cost_usd=0.0,
        usage={},
    )


def test_parse_issue_update_extracts_body_between_markers() -> None:
    text = (
        'preamble\n'
        '<<<ISSUE_UPDATE>>>\n'
        '## Acceptance Criteria\n- new item\n'
        '<<<END_ISSUE_UPDATE>>>\n'
        'trailing\n'
    )
    body = parse_issue_update(text)
    assert body == '## Acceptance Criteria\n- new item'


def test_parse_issue_update_returns_none_when_absent() -> None:
    assert parse_issue_update('PR: https://github.com/x/y/pull/1') is None


def test_parse_issue_update_requires_end_marker() -> None:
    text = '<<<ISSUE_UPDATE>>>\nbody without end\n'
    assert parse_issue_update(text) is None


def test_apply_marker_posts_audit_comment_and_updates_description() -> None:
    fake = _FakeLinear()
    issue = _issue(description='OLD CONTENT')
    text = 'Some preface.\n<<<ISSUE_UPDATE>>>\nNEW CONTENT\n<<<END_ISSUE_UPDATE>>>\n'
    applied = apply_issue_update_marker(
        linear=fake,  # type: ignore[arg-type]
        issue=issue,
        claude=_result(text),
        agent_id='1',
    )
    assert applied is True
    assert len(fake.comments) == 1
    audit_id, audit_body = fake.comments[0]
    assert audit_id == 'uuid-1'
    assert '**agent-1**: ISSUE_BODY_UPDATED' in audit_body
    assert 'OLD CONTENT' in audit_body
    assert '<details>' in audit_body
    assert len(fake.updates) == 1
    update_id, update = fake.updates[0]
    assert update_id == 'uuid-1'
    assert update.description == 'NEW CONTENT'


def test_apply_marker_no_op_when_marker_absent() -> None:
    fake = _FakeLinear()
    applied = apply_issue_update_marker(
        linear=fake,  # type: ignore[arg-type]
        issue=_issue(),
        claude=_result('PR: https://github.com/x/y/pull/1'),
        agent_id='1',
    )
    assert applied is False
    assert fake.comments == []
    assert fake.updates == []


def test_apply_marker_no_op_when_body_unchanged() -> None:
    fake = _FakeLinear()
    issue = _issue(description='SAME')
    text = '<<<ISSUE_UPDATE>>>\nSAME\n<<<END_ISSUE_UPDATE>>>\n'
    applied = apply_issue_update_marker(
        linear=fake,  # type: ignore[arg-type]
        issue=issue,
        claude=_result(text),
        agent_id='1',
    )
    assert applied is False
    assert fake.updates == []


def test_apply_marker_ignores_empty_body() -> None:
    fake = _FakeLinear()
    text = '<<<ISSUE_UPDATE>>>\n   \n<<<END_ISSUE_UPDATE>>>\n'
    applied = apply_issue_update_marker(
        linear=fake,  # type: ignore[arg-type]
        issue=_issue(),
        claude=_result(text),
        agent_id='1',
    )
    assert applied is False
    assert fake.updates == []


def test_decomposition_parser_unaffected_by_issue_update_block() -> None:
    """An ISSUE_UPDATE block in the same output must not corrupt JSON parsing
    of DECOMPOSITION:."""
    grounded = (
        '"context": "ctx", '
        '"implementation_notes": "Touch `src/foo.py`.", '
        '"files_to_touch": ["src/foo.py"], '
        '"relevant_symbols": ["Foo.bar"], '
    )
    text = (
        '<<<ISSUE_UPDATE>>>\n'
        'rewritten body\n'
        '<<<END_ISSUE_UPDATE>>>\n'
        '\nDECOMPOSITION:\n'
        '```json\n'
        '{"rationale": "split", "children": ['
        '{"title": "A", ' + grounded + '"acceptance_criteria": ["x"], "estimate": 1},'
        '{"title": "B", ' + grounded + '"acceptance_criteria": ["y"], "estimate": 2}'
        ']}\n'
        '```\n'
    )
    decomp = parse_decomposition(text)
    assert len(decomp.children) == 2
    assert decomp.children[0].title == 'A'
    assert parse_issue_update(text) == 'rewritten body'
