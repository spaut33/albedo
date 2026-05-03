"""Tests for the Awaiting-approval re-dispatch loop (Part B)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from albedo.comment_redispatch import (
    LAST_COMMENT_STATE_FILE,
    redispatch_on_new_user_comments,
)
from albedo.linear_client import Comment, Issue, IssueUpdate


def _issue(
    *,
    identifier: str = 'AI-5',
    issue_id: str = 'uuid-1',
    label_names: tuple[str, ...] = (),
    label_ids: tuple[str, ...] = (),
    assignee_id: str | None = None,
    state_name: str = 'Awaiting approval',
    parent_id: str | None = None,
) -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title='X',
        description='',
        state_id='state-await',
        state_name=state_name,
        assignee_id=assignee_id,
        label_ids=label_ids,
        label_names=label_names,
        parent_id=parent_id,
        branch_name='',
    )


class _FakeLinear:
    """Minimal LinearClient surface used by comment_redispatch."""

    def __init__(
        self,
        *,
        issues: list[Issue] | None = None,
        comments: dict[str, list[Comment]] | None = None,
        children: dict[str, list[Issue]] | None = None,
    ) -> None:
        self.issues = issues or []
        self.comments = comments or {}
        self.children = children or {}
        self.team_states: dict[str, str] = {
            'Triage': 'state-triage',
            'Backlog': 'state-backlog',
            'Review': 'state-review',
            'Awaiting approval': 'state-await',
        }
        self.team_labels: dict[str, str] = {
            'kind:decomposition': 'lab-decomp',
            'kind:final-pr': 'lab-final',
            'stuck': 'lab-stuck',
            'blocked-external': 'lab-blocked-ext',
            'awaiting-human-reply': 'lab-await-human',
        }
        self.updates: list[tuple[str, IssueUpdate]] = []
        self.archived: list[str] = []

    def list_comments(self, issue_id: str) -> list[Comment]:
        return list(self.comments.get(issue_id, []))

    def list_children(self, parent_id: str) -> list[Issue]:
        return list(self.children.get(parent_id, []))

    def archive_issue(self, issue_id: str) -> None:
        self.archived.append(issue_id)

    def update_issue(self, issue_id: str, update: IssueUpdate) -> Issue:
        self.updates.append((issue_id, update))
        return self.issues[0]

    def _issue_node(self, issue: Issue) -> dict[str, object]:
        return {
            'id': issue.id,
            'identifier': issue.identifier,
            'title': issue.title,
            'description': issue.description,
            'branchName': issue.branch_name,
            'state': {'id': issue.state_id, 'name': issue.state_name},
            'assignee': (
                {'id': issue.assignee_id} if issue.assignee_id is not None else None
            ),
            'parent': {'id': issue.parent_id} if issue.parent_id is not None else None,
            'labels': {
                'nodes': [
                    {'id': lid, 'name': name}
                    for lid, name in zip(
                        issue.label_ids, issue.label_names, strict=True
                    )
                ]
            },
            'inverseRelations': {'nodes': []},
        }

    def query(self, document: str, variables: object) -> dict[str, object]:
        if 'HumanGated' in document or 'issues(filter:' in document:
            vars_dict = cast('dict[str, Any]', variables) if variables else {}
            filt = cast('dict[str, Any]', vars_dict.get('filter', {}))
            state_obj = cast('dict[str, Any]', filt.get('state', {}))
            name_obj = cast('dict[str, Any]', state_obj.get('name', {}))
            state_filter = cast('list[str]', name_obj.get('in', []))
            picked = [i for i in self.issues if i.state_name in state_filter]
            return {'issues': {'nodes': [self._issue_node(i) for i in picked]}}
        if 'team {' in document and 'labels(' in document:
            return {
                'issue': {
                    'team': {
                        'labels': {
                            'nodes': [
                                {'id': lid, 'name': name}
                                for name, lid in self.team_labels.items()
                            ]
                        }
                    }
                }
            }
        return {
            'team': {
                'states': {
                    'nodes': [
                        {'id': sid, 'name': name}
                        for name, sid in self.team_states.items()
                    ]
                }
            }
        }


def _comment(comment_id: str, body: str, author: str = 'human-1') -> Comment:
    return Comment(id=comment_id, body=body, author_id=author)


def test_no_issues_no_op(tmp_path: Path) -> None:
    fake = _FakeLinear()
    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset(),
        state_dir=tmp_path,
    )
    assert report.fired == ()
    assert report.inspected == 0


def test_first_observation_seeds_without_firing(tmp_path: Path) -> None:
    issue = _issue(label_names=('kind:final-pr',), label_ids=('lab-final',))
    fake = _FakeLinear(
        issues=[issue],
        comments={'uuid-1': [_comment('c1', 'looks great', 'human-1')]},
    )
    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset(),
        state_dir=tmp_path,
    )
    assert report.fired == ()
    assert 'AI-5' in report.seeded
    assert fake.updates == []
    assert (tmp_path / LAST_COMMENT_STATE_FILE).exists()


def test_new_comment_after_seed_redispatches_final_pr_to_review(
    tmp_path: Path,
) -> None:
    issue = _issue(label_names=('kind:final-pr',), label_ids=('lab-final',))
    fake = _FakeLinear(
        issues=[issue],
        comments={'uuid-1': [_comment('c1', 'first', 'human-1')]},
    )
    redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset(),
        state_dir=tmp_path,
    )
    fake.comments['uuid-1'].append(_comment('c2', 'rename X to Y', 'human-1'))
    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset(),
        state_dir=tmp_path,
    )
    assert len(report.fired) == 1
    assert report.fired[0].to_state == 'Review'
    assert report.fired[0].trigger_label == 'kind:final-pr'
    update_id, update = fake.updates[0]
    assert update_id == 'uuid-1'
    assert update.state_id == 'state-review'
    assert update.unset_assignee is True
    assert 'lab-final' not in (update.label_ids or ())


def test_stuck_label_redispatches_to_backlog_and_strips_label(
    tmp_path: Path,
) -> None:
    issue = _issue(label_names=('stuck',), label_ids=('lab-stuck',))
    fake = _FakeLinear(
        issues=[issue],
        comments={
            'uuid-1': [
                _comment('c1', 'first', 'human-1'),
                _comment('c2', 'try again', 'human-1'),
            ]
        },
    )
    # Pre-seed so the second comment counts as "new".
    (tmp_path / LAST_COMMENT_STATE_FILE).write_text(
        '{"version": 1, "seen": {"uuid-1": "c1"}}', encoding='utf-8'
    )
    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset(),
        state_dir=tmp_path,
    )
    assert report.fired[0].to_state == 'Backlog'
    _update_id, update = fake.updates[0]
    assert update.state_id == 'state-backlog'
    assert 'lab-stuck' not in (update.label_ids or ())


def test_decomposition_archives_children_and_returns_to_triage(
    tmp_path: Path,
) -> None:
    parent = _issue(
        identifier='AI-1',
        issue_id='parent-uuid',
        label_names=('kind:decomposition',),
        label_ids=('lab-decomp',),
    )
    child_a = _issue(identifier='AI-2', issue_id='child-a')
    child_b = _issue(identifier='AI-3', issue_id='child-b')
    fake = _FakeLinear(
        issues=[parent],
        comments={
            'parent-uuid': [
                _comment('c1', 'old', 'human-1'),
                _comment('c2', 'split differently', 'human-1'),
            ]
        },
        children={'parent-uuid': [child_a, child_b]},
    )
    (tmp_path / LAST_COMMENT_STATE_FILE).write_text(
        '{"version": 1, "seen": {"parent-uuid": "c1"}}', encoding='utf-8'
    )
    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset(),
        state_dir=tmp_path,
    )
    assert report.fired[0].to_state == 'Triage'
    assert sorted(fake.archived) == ['child-a', 'child-b']
    update = fake.updates[0][1]
    assert update.state_id == 'state-triage'
    assert 'lab-decomp' not in (update.label_ids or ())


def test_bot_comments_filtered_out(tmp_path: Path) -> None:
    issue = _issue(label_names=('kind:final-pr',), label_ids=('lab-final',))
    fake = _FakeLinear(
        issues=[issue],
        comments={
            'uuid-1': [_comment('c1', '**agent-1**: PR: https://x/y/1', 'bot-1')]
        },
    )
    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset({'bot-1'}),
        state_dir=tmp_path,
    )
    assert report.fired == ()
    assert report.seeded == ()


def test_redispatch_skips_when_no_recognised_label(tmp_path: Path) -> None:
    issue = _issue(label_names=('foo',), label_ids=('lab-foo',))
    fake = _FakeLinear(
        issues=[issue],
        comments={
            'uuid-1': [
                _comment('c1', 'first', 'human-1'),
                _comment('c2', 'second', 'human-1'),
            ]
        },
    )
    (tmp_path / LAST_COMMENT_STATE_FILE).write_text(
        '{"version": 1, "seen": {"uuid-1": "c1"}}', encoding='utf-8'
    )
    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset(),
        state_dir=tmp_path,
    )
    assert report.fired == ()
    assert fake.updates == []


def test_assigned_issues_skipped(tmp_path: Path) -> None:
    """list_pickup_issues filters assignee=null upstream — we rely on it."""
    fake = _FakeLinear(
        issues=[],
        comments={
            'uuid-1': [_comment('c1', 'first', 'human-1')],
        },
    )
    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset(),
        state_dir=tmp_path,
    )
    assert report.inspected == 0
    assert fake.updates == []


def test_state_file_idempotent_on_repeated_runs(tmp_path: Path) -> None:
    issue = _issue(label_names=('kind:final-pr',), label_ids=('lab-final',))
    fake = _FakeLinear(
        issues=[issue],
        comments={
            'uuid-1': [
                _comment('c1', 'first', 'human-1'),
                _comment('c2', 'second', 'human-1'),
            ]
        },
    )
    (tmp_path / LAST_COMMENT_STATE_FILE).write_text(
        '{"version": 1, "seen": {"uuid-1": "c1"}}', encoding='utf-8'
    )
    redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset(),
        state_dir=tmp_path,
    )
    assert len(fake.updates) == 1
    second = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset(),
        state_dir=tmp_path,
    )
    # No new fires: latest comment id matches stored.
    assert second.fired == ()
    assert len(fake.updates) == 1


def test_triage_awaiting_human_reply_strips_label_in_place(
    tmp_path: Path,
) -> None:
    """Architect BLOCKED with a question lands the issue in Triage with
    `awaiting-human-reply`. A new user comment strips the label so the
    next worker poll re-picks the issue."""
    issue = _issue(
        state_name='Triage',
        label_names=('awaiting-human-reply',),
        label_ids=('lab-await-human',),
    )
    fake = _FakeLinear(
        issues=[issue],
        comments={
            'uuid-1': [
                _comment('c1', 'BLOCKED comment from agent', 'human-1'),
                _comment('c2', 'use SQLite', 'human-1'),
            ]
        },
    )
    (tmp_path / LAST_COMMENT_STATE_FILE).write_text(
        '{"version": 1, "seen": {"uuid-1": "c1"}}', encoding='utf-8'
    )
    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset(),
        state_dir=tmp_path,
    )
    assert len(report.fired) == 1
    fired = report.fired[0]
    assert fired.from_state == 'Triage'
    assert fired.to_state == 'Triage'
    assert fired.trigger_label == 'awaiting-human-reply'
    _, update = fake.updates[0]
    # No state change — only label-strip + assignee unset.
    assert update.state_id is None
    assert update.unset_assignee is True
    assert 'lab-await-human' not in (update.label_ids or ())


def test_backlog_awaiting_human_reply_strips_label_in_place(
    tmp_path: Path,
) -> None:
    issue = _issue(
        state_name='Backlog',
        label_names=('awaiting-human-reply',),
        label_ids=('lab-await-human',),
    )
    fake = _FakeLinear(
        issues=[issue],
        comments={
            'uuid-1': [
                _comment('c1', 'BLOCKED', 'human-1'),
                _comment('c2', 'AC update: ...', 'human-1'),
            ]
        },
    )
    (tmp_path / LAST_COMMENT_STATE_FILE).write_text(
        '{"version": 1, "seen": {"uuid-1": "c1"}}', encoding='utf-8'
    )
    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset(),
        state_dir=tmp_path,
    )
    assert report.fired[0].to_state == 'Backlog'
    assert report.fired[0].trigger_label == 'awaiting-human-reply'
    _, update = fake.updates[0]
    assert update.state_id is None
    assert update.unset_assignee is True
    assert 'lab-await-human' not in (update.label_ids or ())


def test_triage_child_with_label_skipped(tmp_path: Path) -> None:
    """A Triage child (parent_id set) is gated by parent approval, not by
    user comments on itself — leave it alone even if labelled."""
    issue = _issue(
        state_name='Triage',
        parent_id='parent-uuid',
        label_names=('awaiting-human-reply',),
        label_ids=('lab-await-human',),
    )
    fake = _FakeLinear(
        issues=[issue],
        comments={
            'uuid-1': [
                _comment('c1', 'one', 'human-1'),
                _comment('c2', 'two', 'human-1'),
            ]
        },
    )
    (tmp_path / LAST_COMMENT_STATE_FILE).write_text(
        '{"version": 1, "seen": {"uuid-1": "c1"}}', encoding='utf-8'
    )
    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset(),
        state_dir=tmp_path,
    )
    assert report.fired == ()
    assert fake.updates == []


def test_ungated_triage_or_backlog_not_redispatched(tmp_path: Path) -> None:
    """Plain Triage/Backlog without `awaiting-human-reply` is naturally
    re-pickable by workers — redispatch must not touch it."""
    triage = _issue(
        identifier='AI-10',
        issue_id='uuid-10',
        state_name='Triage',
    )
    backlog = _issue(
        identifier='AI-11',
        issue_id='uuid-11',
        state_name='Backlog',
    )
    fake = _FakeLinear(
        issues=[triage, backlog],
        comments={
            'uuid-10': [
                _comment('c1', 'one', 'human-1'),
                _comment('c2', 'two', 'human-1'),
            ],
            'uuid-11': [
                _comment('c3', 'three', 'human-1'),
                _comment('c4', 'four', 'human-1'),
            ],
        },
    )
    (tmp_path / LAST_COMMENT_STATE_FILE).write_text(
        '{"version": 1, "seen": {"uuid-10": "c1", "uuid-11": "c3"}}',
        encoding='utf-8',
    )
    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset(),
        state_dir=tmp_path,
    )
    assert report.fired == ()
    assert fake.updates == []


def test_awaiting_human_reply_fires_on_first_observation(tmp_path: Path) -> None:
    """The architect just set `awaiting-human-reply`; the user replied
    once. We must fire immediately — seed-without-fire would leave the
    issue stuck. Rollout protection only applies to Awaiting approval,
    where historical comments may pre-date the feature."""
    issue = _issue(
        state_name='Triage',
        label_names=('awaiting-human-reply',),
        label_ids=('lab-await-human',),
    )
    fake = _FakeLinear(
        issues=[issue],
        comments={
            'uuid-1': [
                _comment('c1', '**agent-1**: BLOCKED: which lib?', 'bot-1'),
                _comment('c2', "let's use textual", 'human-1'),
            ]
        },
    )
    # No prior state — first time housekeeping sees this gated issue.
    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset({'bot-1'}),
        state_dir=tmp_path,
    )
    assert len(report.fired) == 1
    assert report.fired[0].trigger_label == 'awaiting-human-reply'
    _, update = fake.updates[0]
    assert 'lab-await-human' not in (update.label_ids or ())


_TriageCalls = list[tuple[str, tuple[str, ...]]]


def _record_triage(
    verdict: bool,
) -> tuple[Any, _TriageCalls]:
    """Build a triage stub that always returns `verdict` and records calls."""
    calls: _TriageCalls = []

    def fn(agent_message: str, user_replies: Sequence[str]) -> bool:
        calls.append((agent_message, tuple(user_replies)))
        return verdict

    return fn, calls


def test_triage_yes_fires_redispatch(tmp_path: Path) -> None:
    issue = _issue(
        state_name='Triage',
        label_names=('awaiting-human-reply',),
        label_ids=('lab-await-human',),
    )
    fake = _FakeLinear(
        issues=[issue],
        comments={
            'uuid-1': [
                _comment('c1', '**agent-1**: BLOCKED: which DB?', 'bot-1'),
                _comment('c2', 'use SQLite', 'human-1'),
            ]
        },
    )
    triage_fn, calls = _record_triage(verdict=True)
    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset({'bot-1'}),
        state_dir=tmp_path,
        triage_fn=triage_fn,  # type: ignore[arg-type]
    )
    assert len(report.fired) == 1
    assert report.skipped_non_substantive == ()
    assert len(calls) == 1
    agent_message, user_replies = calls[0]
    assert 'which DB?' in agent_message
    assert user_replies == ('use SQLite',)


def test_triage_no_skips_and_pins_seen(tmp_path: Path) -> None:
    issue = _issue(
        state_name='Triage',
        label_names=('awaiting-human-reply',),
        label_ids=('lab-await-human',),
    )
    fake = _FakeLinear(
        issues=[issue],
        comments={
            'uuid-1': [
                _comment('c1', '**agent-1**: BLOCKED: which DB?', 'bot-1'),
                _comment('c2', 'надо подумать...', 'human-1'),
            ]
        },
    )
    triage_fn, calls = _record_triage(verdict=False)
    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset({'bot-1'}),
        state_dir=tmp_path,
        triage_fn=triage_fn,  # type: ignore[arg-type]
    )
    assert report.fired == ()
    assert report.skipped_non_substantive == ('AI-5',)
    assert fake.updates == []
    assert len(calls) == 1

    # Second tick on the same comment: triage must NOT be called again
    # (seen pinned to c2), and nothing fires.
    second = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset({'bot-1'}),
        state_dir=tmp_path,
        triage_fn=triage_fn,  # type: ignore[arg-type]
    )
    assert second.fired == ()
    assert second.skipped_non_substantive == ()
    assert len(calls) == 1


def test_triage_re_evaluates_on_new_comment_after_skip(tmp_path: Path) -> None:
    """A user follow-up after a non-substantive reply gets re-triaged."""
    issue = _issue(
        state_name='Triage',
        label_names=('awaiting-human-reply',),
        label_ids=('lab-await-human',),
    )
    fake = _FakeLinear(
        issues=[issue],
        comments={
            'uuid-1': [
                _comment('c1', '**agent-1**: BLOCKED: which DB?', 'bot-1'),
                _comment('c2', 'надо подумать', 'human-1'),
            ]
        },
    )
    verdict_seq = iter([False, True])
    calls: list[tuple[str, tuple[str, ...]]] = []

    def triage_fn(agent_message: str, user_replies: Sequence[str]) -> bool:
        calls.append((agent_message, tuple(user_replies)))
        return next(verdict_seq)

    redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset({'bot-1'}),
        state_dir=tmp_path,
        triage_fn=triage_fn,
    )
    fake.comments['uuid-1'].append(_comment('c3', 'fine, use SQLite', 'human-1'))
    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset({'bot-1'}),
        state_dir=tmp_path,
        triage_fn=triage_fn,
    )
    assert len(report.fired) == 1
    # Triage was called twice: once per distinct latest-comment id.
    assert len(calls) == 2
    # Second call sees both replies, in order.
    assert calls[1][1] == ('надо подумать', 'fine, use SQLite')


def test_triage_callback_exception_fails_open(tmp_path: Path) -> None:
    issue = _issue(
        state_name='Triage',
        label_names=('awaiting-human-reply',),
        label_ids=('lab-await-human',),
    )
    fake = _FakeLinear(
        issues=[issue],
        comments={
            'uuid-1': [
                _comment('c1', '**agent-1**: BLOCKED', 'bot-1'),
                _comment('c2', 'answer', 'human-1'),
            ]
        },
    )

    def boom(_agent: str, _replies: Sequence[str]) -> bool:
        raise RuntimeError('triage exploded')

    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset({'bot-1'}),
        state_dir=tmp_path,
        triage_fn=boom,
    )
    assert len(report.fired) == 1


def test_triage_skipped_when_no_agent_anchor(tmp_path: Path) -> None:
    """If there's no bot comment to anchor on, fire without triage."""
    issue = _issue(
        state_name='Triage',
        label_names=('awaiting-human-reply',),
        label_ids=('lab-await-human',),
    )
    fake = _FakeLinear(
        issues=[issue],
        comments={
            'uuid-1': [_comment('c1', 'random user comment', 'human-1')],
        },
    )
    triage_fn, calls = _record_triage(verdict=False)
    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset({'bot-1'}),
        state_dir=tmp_path,
        triage_fn=triage_fn,  # type: ignore[arg-type]
    )
    # Fired despite triage NO — because triage was bypassed (no anchor).
    assert len(report.fired) == 1
    assert calls == []


def test_no_triage_fn_preserves_legacy_behavior(tmp_path: Path) -> None:
    """Omitting triage_fn keeps the always-fire-on-new-comment behavior."""
    issue = _issue(
        state_name='Triage',
        label_names=('awaiting-human-reply',),
        label_ids=('lab-await-human',),
    )
    fake = _FakeLinear(
        issues=[issue],
        comments={
            'uuid-1': [
                _comment('c1', '**agent-1**: BLOCKED', 'bot-1'),
                _comment('c2', 'ok', 'human-1'),
            ]
        },
    )
    report = redispatch_on_new_user_comments(
        linear=fake,  # type: ignore[arg-type]
        team_id='t1',
        bot_user_ids=frozenset({'bot-1'}),
        state_dir=tmp_path,
    )
    assert len(report.fired) == 1
