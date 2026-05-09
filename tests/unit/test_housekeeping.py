"""Tests for stale-claim recovery on startup."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from albedo.heartbeat import heartbeat_path, touch_heartbeat
from albedo.housekeeping import recover_stale_claims
from albedo.linear_client import Issue, IssueUpdate


def _issue(
    *,
    identifier: str = 'AI-5',
    label_names: tuple[str, ...] = (),
    branch_name: str = '',
) -> Issue:
    return Issue(
        id=f'uuid-{identifier}',
        identifier=identifier,
        title='X',
        description='',
        state_id='s',
        state_name='Backlog',
        assignee_id='agent-user',
        label_ids=(),
        label_names=label_names,
        parent_id=None,
        branch_name=branch_name,
    )


class _FakeLinear:
    def __init__(self, assigned: list[Issue]) -> None:
        self._assigned = assigned
        self.released: list[Issue] = []
        self.updates: list[tuple[str, IssueUpdate]] = []

    def list_assigned_issues(
        self, _user_id: str, *, project_id: str | None = None
    ) -> list[Issue]:
        del project_id
        return self._assigned

    def update_issue(self, issue_id: str, update: IssueUpdate) -> Issue:
        self.updates.append((issue_id, update))
        assert update.unset_assignee is True
        for issue in self._assigned:
            if issue.id == issue_id:
                self.released.append(issue)
                return issue
        raise AssertionError('Unknown issue id')


def _age(path: Path, seconds: int) -> None:
    when = (datetime.now(UTC) - timedelta(seconds=seconds)).timestamp()
    os.utime(path, (when, when))


def test_releases_when_no_heartbeat_exists(tmp_path: Path) -> None:
    fake = _FakeLinear([_issue(label_names=('agent-1',))])
    report = recover_stale_claims(
        linear=fake,  # type: ignore[arg-type]
        state_dir=tmp_path,
        agent_user_id='agent-user',
    )
    assert report.inspected == 1
    assert len(report.released) == 1
    assert len(fake.released) == 1


def test_releases_when_heartbeat_is_stale(tmp_path: Path) -> None:
    state = tmp_path / 'state'
    hb = heartbeat_path(state, '1')
    touch_heartbeat(hb)
    _age(hb, seconds=600)
    fake = _FakeLinear([_issue(label_names=('agent-1',))])
    report = recover_stale_claims(
        linear=fake,  # type: ignore[arg-type]
        state_dir=state,
        agent_user_id='agent-user',
        max_heartbeat_age_seconds=300,
    )
    assert len(report.released) == 1


def test_keeps_when_heartbeat_is_fresh(tmp_path: Path) -> None:
    state = tmp_path / 'state'
    touch_heartbeat(heartbeat_path(state, '1'))
    fake = _FakeLinear([_issue(label_names=('agent-1',))])
    report = recover_stale_claims(
        linear=fake,  # type: ignore[arg-type]
        state_dir=state,
        agent_user_id='agent-user',
    )
    assert report.released == []


def test_active_agent_ids_treated_as_fresh(tmp_path: Path) -> None:
    fake = _FakeLinear([_issue(label_names=('agent-2',))])
    report = recover_stale_claims(
        linear=fake,  # type: ignore[arg-type]
        state_dir=tmp_path,
        agent_user_id='agent-user',
        active_agent_ids=['2'],
    )
    assert report.released == []


def test_extracts_agent_from_branch_name(tmp_path: Path) -> None:
    state = tmp_path / 'state'
    fake = _FakeLinear([_issue(branch_name='feature/agent-3/work')])
    report = recover_stale_claims(
        linear=fake,  # type: ignore[arg-type]
        state_dir=state,
        agent_user_id='agent-user',
    )
    assert len(report.released) == 1


def test_no_agent_marker_keeps_when_some_worker_fresh(
    tmp_path: Path,
) -> None:
    state = tmp_path / 'state'
    touch_heartbeat(heartbeat_path(state, '1'))
    fake = _FakeLinear([_issue()])
    report = recover_stale_claims(
        linear=fake,  # type: ignore[arg-type]
        state_dir=state,
        agent_user_id='agent-user',
    )
    assert report.released == []


def test_no_agent_marker_releases_when_all_stale(tmp_path: Path) -> None:
    state = tmp_path / 'state'
    hb = heartbeat_path(state, '1')
    touch_heartbeat(hb)
    _age(hb, seconds=600)
    fake = _FakeLinear([_issue()])
    report = recover_stale_claims(
        linear=fake,  # type: ignore[arg-type]
        state_dir=state,
        agent_user_id='agent-user',
        max_heartbeat_age_seconds=300,
    )
    assert len(report.released) == 1


def test_no_agent_marker_releases_when_no_heartbeats_dir(tmp_path: Path) -> None:
    fake = _FakeLinear([_issue()])
    report = recover_stale_claims(
        linear=fake,  # type: ignore[arg-type]
        state_dir=tmp_path / 'missing',
        agent_user_id='agent-user',
    )
    assert len(report.released) == 1


def test_recover_restores_state_from_manifest(tmp_path: Path) -> None:
    from albedo.claim_manifest import (
        ClaimManifest,
        claim_manifest_path,
        write_claim_manifest,
    )

    state_dir = tmp_path / 'state'
    state_dir.mkdir()
    issue = _issue(label_names=('agent-1',))
    write_claim_manifest(
        claim_manifest_path(state_dir, '1'),
        ClaimManifest(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            prev_state_id='state-backlog',
            prev_state_name='Backlog',
            claimed_at_unix=1.0,
        ),
    )
    fake = _FakeLinear([issue])
    report = recover_stale_claims(
        linear=fake,  # type: ignore[arg-type]
        state_dir=state_dir,
        agent_user_id='agent-user',
    )
    assert len(report.released) == 1
    assert len(fake.updates) == 1
    _, update = fake.updates[0]
    assert update.state_id == 'state-backlog'
    assert update.unset_assignee is True
    assert not claim_manifest_path(state_dir, '1').exists()


def test_recover_falls_back_to_assignee_only_without_manifest(tmp_path: Path) -> None:
    fake = _FakeLinear([_issue(label_names=('agent-1',))])
    report = recover_stale_claims(
        linear=fake,  # type: ignore[arg-type]
        state_dir=tmp_path,
        agent_user_id='agent-user',
    )
    assert len(report.released) == 1
    _, update = fake.updates[0]
    assert update.state_id is None
    assert update.unset_assignee is True


# --- Phase 5 / Variant A: decomposition approval watcher --------------------


from albedo.housekeeping import release_decomposed_children  # noqa: E402

_PARENT_STATE_TYPES: dict[str, str] = {
    'state-todo': 'unstarted',
    'state-done': 'completed',
    'state-backlog': 'unstarted',
    'state-triage': 'triage',
    'state-canceled': 'canceled',
}


class _WatcherFakeLinear:
    def __init__(
        self,
        *,
        parents: list[Issue],
        children_by_parent: dict[str, list[Issue]],
        team_states: dict[str, str],
    ) -> None:
        self.parents = parents
        self.children_by_parent = children_by_parent
        self.team_states = team_states
        self.updates: list[tuple[str, IssueUpdate]] = []
        self.comments: list[tuple[str, str]] = []

    def query(self, document: str, variables: dict[str, Any]) -> dict[str, object]:
        if 'DecompositionParents' in document:
            wanted_type: str = variables['filter']['state']['type']['eq']
            matching = [
                p
                for p in self.parents
                if _PARENT_STATE_TYPES.get(p.state_id, 'unstarted') == wanted_type
            ]
            return {
                'issues': {
                    'nodes': [
                        {
                            'id': p.id,
                            'identifier': p.identifier,
                            'title': p.title,
                            'description': p.description,
                            'branchName': p.branch_name,
                            'state': {
                                'id': p.state_id,
                                'name': p.state_name,
                            },
                            'assignee': (
                                {'id': p.assignee_id} if p.assignee_id else None
                            ),
                            'parent': ({'id': p.parent_id} if p.parent_id else None),
                            'labels': {
                                'nodes': [
                                    {'id': lid, 'name': name}
                                    for lid, name in zip(
                                        p.label_ids,
                                        p.label_names,
                                        strict=False,
                                    )
                                ]
                            },
                        }
                        for p in matching
                    ]
                }
            }
        if 'StateByName' in document:
            return {
                'workflowStates': {
                    'nodes': [
                        {'id': sid, 'name': name}
                        for name, sid in self.team_states.items()
                    ]
                }
            }
        raise AssertionError(f'Unexpected query: {document[:120]}')

    def list_children(self, parent_id: str) -> list[Issue]:
        return list(self.children_by_parent.get(parent_id, []))

    def update_issue(self, issue_id: str, update: IssueUpdate) -> Issue:
        self.updates.append((issue_id, update))
        return Issue(
            id=issue_id,
            identifier='X',
            title='',
            description='',
            state_id=update.state_id or 's',
            state_name='Backlog',
            assignee_id=None,
            label_ids=update.label_ids or (),
            label_names=(),
            parent_id=None,
            branch_name='',
        )

    def add_comment(self, issue_id: str, body: str) -> str:
        self.comments.append((issue_id, body))
        return 'c1'


def _parent(
    *,
    label_ids: tuple[str, ...] = (),
    state_name: str = 'Todo',
    state_id: str = 'state-todo',
) -> Issue:
    return Issue(
        id='uuid-parent',
        identifier='AI-50',
        title='Feature X',
        description='',
        state_id=state_id,
        state_name=state_name,
        assignee_id=None,
        label_ids=label_ids,
        label_names=(),
        parent_id=None,
        branch_name='',
    )


def _child(
    *,
    suffix: str,
    state_name: str = 'Triage',
    label_names: tuple[str, ...] = (),
) -> Issue:
    return Issue(
        id=f'uuid-c-{suffix}',
        identifier=f'AI-{suffix}',
        title=f'child {suffix}',
        description='',
        state_id=f'state-{state_name.lower().replace(" ", "-")}',
        state_name=state_name,
        assignee_id=None,
        label_ids=(),
        label_names=label_names,
        parent_id='uuid-parent',
        branch_name='',
    )


_DEFAULT_STATES = {
    'Triage': 'state-triage',
    'Backlog': 'state-backlog',
    'Todo': 'state-todo',
    'Done': 'state-done',
    'Canceled': 'state-canceled',
}


def test_release_decomposed_children_moves_triage_to_backlog() -> None:
    parents = [_parent(label_ids=('lab-kind-decomp',))]
    children = {
        'uuid-parent': [
            _child(suffix='51'),
            _child(suffix='52'),
            _child(suffix='53', state_name='Backlog'),
        ]
    }
    fake = _WatcherFakeLinear(
        parents=parents,
        children_by_parent=children,
        team_states=_DEFAULT_STATES,
    )

    report = release_decomposed_children(linear=fake, team_id='t1')  # type: ignore[arg-type]

    assert report.parents_processed == 1
    assert [c.identifier for c in report.children_released] == ['AI-51', 'AI-52']
    assert len(fake.updates) == 2
    for _, update in fake.updates:
        assert update.state_id == 'state-backlog'
    assert fake.comments[0][0] == 'uuid-parent'
    assert 'AI-51' in fake.comments[0][1]


def test_release_decomposed_children_idempotent_when_already_in_backlog() -> None:
    parents = [_parent(label_ids=('lab-kind-decomp',))]
    children = {
        'uuid-parent': [
            _child(suffix='51', state_name='Backlog'),
        ]
    }
    fake = _WatcherFakeLinear(
        parents=parents,
        children_by_parent=children,
        team_states=_DEFAULT_STATES,
    )

    report = release_decomposed_children(linear=fake, team_id='t1')  # type: ignore[arg-type]
    assert report.children_released == []
    assert fake.updates == []
    assert fake.comments == []


def test_release_decomposed_children_skips_when_no_backlog_state() -> None:
    parents = [_parent(label_ids=('lab-kind-decomp',))]
    fake = _WatcherFakeLinear(
        parents=parents,
        children_by_parent={'uuid-parent': []},
        team_states={'Triage': 'state-triage'},  # no Backlog
    )
    report = release_decomposed_children(linear=fake, team_id='t1')  # type: ignore[arg-type]
    assert report.children_released == []


def test_release_decomposed_children_cancels_human_marked_child() -> None:
    parents = [_parent(label_ids=('lab-kind-decomp',))]
    children = {
        'uuid-parent': [
            _child(suffix='51'),
            _child(suffix='52', label_names=('cancelled-by-human',)),
        ]
    }
    fake = _WatcherFakeLinear(
        parents=parents,
        children_by_parent=children,
        team_states=_DEFAULT_STATES,
    )

    report = release_decomposed_children(linear=fake, team_id='t1')  # type: ignore[arg-type]

    assert [c.identifier for c in report.children_released] == ['AI-51']
    assert [c.identifier for c in report.children_cancelled] == ['AI-52']
    target_states = {issue_id: u.state_id for issue_id, u in fake.updates}
    assert target_states['uuid-c-51'] == 'state-backlog'
    assert target_states['uuid-c-52'] == 'state-canceled'
    assert len(fake.comments) == 1
    body = fake.comments[0][1]
    assert 'released AI-51' in body
    assert 'cancelled AI-52' in body


def test_release_decomposed_children_ignores_parent_in_completed_state() -> None:
    parents = [
        _parent(
            label_ids=('lab-kind-decomp',),
            state_name='Done',
            state_id='state-done',
        )
    ]
    children = {'uuid-parent': [_child(suffix='51')]}
    fake = _WatcherFakeLinear(
        parents=parents,
        children_by_parent=children,
        team_states=_DEFAULT_STATES,
    )

    report = release_decomposed_children(linear=fake, team_id='t1')  # type: ignore[arg-type]

    assert report.parents_processed == 0
    assert report.children_released == []
    assert report.children_cancelled == []
    assert fake.updates == []
    assert fake.comments == []


# --- AI-101: decomposition parent rollup to Done ---------------------------

from albedo.housekeeping import (  # noqa: E402
    complete_decompositions_when_children_done,
)


def test_rollup_closes_parent_when_all_children_finished() -> None:
    parents = [_parent(label_ids=('lab-kind-decomp',))]
    children = {
        'uuid-parent': [
            _child(suffix='51', state_name='Done'),
            _child(suffix='52', state_name='Canceled'),
            _child(suffix='53', state_name='Duplicate'),
        ]
    }
    fake = _WatcherFakeLinear(
        parents=parents,
        children_by_parent=children,
        team_states=_DEFAULT_STATES,
    )

    report = complete_decompositions_when_children_done(linear=fake, team_id='t1')  # type: ignore[arg-type]

    assert [p.identifier for p in report.parents_completed] == ['AI-50']
    assert fake.updates == [('uuid-parent', IssueUpdate(state_id='state-done'))]
    assert len(fake.comments) == 1
    parent_id, body = fake.comments[0]
    assert parent_id == 'uuid-parent'
    assert 'All decomposition children finished' in body


def test_rollup_leaves_parent_when_a_child_still_unfinished() -> None:
    parents = [_parent(label_ids=('lab-kind-decomp',))]
    children = {
        'uuid-parent': [
            _child(suffix='51', state_name='Done'),
            _child(suffix='52', state_name='Backlog'),
        ]
    }
    fake = _WatcherFakeLinear(
        parents=parents,
        children_by_parent=children,
        team_states=_DEFAULT_STATES,
    )

    report = complete_decompositions_when_children_done(linear=fake, team_id='t1')  # type: ignore[arg-type]

    assert report.parents_completed == []
    assert fake.updates == []
    assert fake.comments == []


def test_rollup_skips_parent_with_zero_children() -> None:
    parents = [_parent(label_ids=('lab-kind-decomp',))]
    fake = _WatcherFakeLinear(
        parents=parents,
        children_by_parent={'uuid-parent': []},
        team_states=_DEFAULT_STATES,
    )

    report = complete_decompositions_when_children_done(linear=fake, team_id='t1')  # type: ignore[arg-type]

    assert report.parents_completed == []
    assert fake.updates == []
    assert fake.comments == []


def test_rollup_idempotent_when_parent_already_done() -> None:
    parents = [
        _parent(
            label_ids=('lab-kind-decomp',),
            state_name='Done',
            state_id='state-done',
        )
    ]
    children = {
        'uuid-parent': [_child(suffix='51', state_name='Done')],
    }
    fake = _WatcherFakeLinear(
        parents=parents,
        children_by_parent=children,
        team_states=_DEFAULT_STATES,
    )

    report = complete_decompositions_when_children_done(linear=fake, team_id='t1')  # type: ignore[arg-type]

    # Parent is in `completed` state type, so the candidate query
    # filtering on type='unstarted' excludes it — no update issued.
    assert report.parents_completed == []
    assert fake.updates == []
    assert fake.comments == []


# --- Phase 6: PR merge → Done watcher ---------------------------------------


from albedo.github_client import GithubError, PullRequest  # noqa: E402
from albedo.housekeeping import sync_merged_prs_to_done  # noqa: E402
from albedo.linear_client import Comment  # noqa: E402


class _MergeSyncFakeLinear:
    def __init__(
        self,
        *,
        candidates: list[Issue],
        comments_by_issue: dict[str, list[Comment]],
        team_states: dict[str, str],
    ) -> None:
        self.candidates = candidates
        self.comments_by_issue = comments_by_issue
        self.team_states = team_states
        self.updates: list[tuple[str, IssueUpdate]] = []

    def query(self, document: str, _variables: object) -> dict[str, object]:
        if 'AwaitingFinalPr' in document:
            return {
                'issues': {
                    'nodes': [
                        {
                            'id': c.id,
                            'identifier': c.identifier,
                            'title': c.title,
                            'description': c.description,
                            'branchName': c.branch_name,
                            'state': {'id': c.state_id, 'name': c.state_name},
                            'assignee': (
                                {'id': c.assignee_id} if c.assignee_id else None
                            ),
                            'parent': ({'id': c.parent_id} if c.parent_id else None),
                            'labels': {
                                'nodes': [
                                    {'id': lid, 'name': name}
                                    for lid, name in zip(
                                        c.label_ids, c.label_names, strict=False
                                    )
                                ]
                            },
                        }
                        for c in self.candidates
                    ]
                }
            }
        if 'StateByName' in document:
            return {
                'workflowStates': {
                    'nodes': [
                        {'id': sid, 'name': name}
                        for name, sid in self.team_states.items()
                    ]
                }
            }
        raise AssertionError(f'Unexpected query: {document[:120]}')

    def list_comments(self, issue_id: str) -> list[Comment]:
        return list(self.comments_by_issue.get(issue_id, []))

    def update_issue(self, issue_id: str, update: IssueUpdate) -> Issue:
        self.updates.append((issue_id, update))
        return Issue(
            id=issue_id,
            identifier='X',
            title='',
            description='',
            state_id=update.state_id or 's',
            state_name='Done',
            assignee_id=None,
            label_ids=(),
            label_names=(),
            parent_id=None,
            branch_name='',
        )


class _StubGithub:
    def __init__(
        self, prs: dict[tuple[str, str, int], PullRequest | Exception]
    ) -> None:
        self.prs = prs

    def get_pull_request(self, owner: str, repo: str, number: int) -> PullRequest:
        result = self.prs.get((owner, repo, number))
        if result is None:
            raise GithubError('not configured in test')
        if isinstance(result, Exception):
            raise result
        return result


def _candidate(*, identifier: str = 'AI-30') -> Issue:
    return Issue(
        id=f'uuid-{identifier}',
        identifier=identifier,
        title=identifier,
        description='',
        state_id='state-await',
        state_name='Awaiting approval',
        assignee_id=None,
        label_ids=('lab-final',),
        label_names=('kind:final-pr',),
        parent_id=None,
        branch_name='',
    )


def _pr_comment(*, body: str) -> Comment:
    return Comment(id='c1', body=body, author_id='u1')


def test_sync_merged_prs_to_done_moves_merged() -> None:
    candidate = _candidate(identifier='AI-30')
    fake_linear = _MergeSyncFakeLinear(
        candidates=[candidate],
        comments_by_issue={
            'uuid-AI-30': [
                _pr_comment(
                    body='**agent-1**: PR: https://github.com/me/sample/pull/42'
                )
            ]
        },
        team_states={
            'Awaiting approval': 'state-await',
            'Done': 'state-done',
        },
    )
    github = _StubGithub(
        {
            ('me', 'sample', 42): PullRequest(
                owner='me', repo='sample', number=42, state='closed', merged=True
            )
        }
    )

    report = sync_merged_prs_to_done(
        linear=fake_linear,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        team_id='t1',
    )
    assert report.inspected == 1
    assert [i.identifier for i in report.moved_to_done] == ['AI-30']
    assert len(fake_linear.updates) == 1
    _, update = fake_linear.updates[0]
    assert update.state_id == 'state-done'


def test_sync_merged_prs_to_done_skips_unmerged() -> None:
    candidate = _candidate()
    fake_linear = _MergeSyncFakeLinear(
        candidates=[candidate],
        comments_by_issue={
            'uuid-AI-30': [_pr_comment(body='PR: https://github.com/me/sample/pull/42')]
        },
        team_states={'Done': 'state-done'},
    )
    github = _StubGithub(
        {
            ('me', 'sample', 42): PullRequest(
                owner='me', repo='sample', number=42, state='open', merged=False
            )
        }
    )

    report = sync_merged_prs_to_done(
        linear=fake_linear,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        team_id='t1',
    )
    assert report.moved_to_done == []
    assert fake_linear.updates == []


def test_sync_merged_prs_to_done_skips_when_no_pr_url() -> None:
    candidate = _candidate()
    fake_linear = _MergeSyncFakeLinear(
        candidates=[candidate],
        comments_by_issue={'uuid-AI-30': []},
        team_states={'Done': 'state-done'},
    )
    github = _StubGithub({})

    report = sync_merged_prs_to_done(
        linear=fake_linear,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        team_id='t1',
    )
    assert report.moved_to_done == []
    assert fake_linear.updates == []


def test_sync_merged_prs_to_done_skips_when_github_errors() -> None:
    candidate = _candidate()
    fake_linear = _MergeSyncFakeLinear(
        candidates=[candidate],
        comments_by_issue={
            'uuid-AI-30': [_pr_comment(body='PR: https://github.com/me/sample/pull/42')]
        },
        team_states={'Done': 'state-done'},
    )
    github = _StubGithub({('me', 'sample', 42): GithubError('rate limit')})

    report = sync_merged_prs_to_done(
        linear=fake_linear,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        team_id='t1',
    )
    assert report.moved_to_done == []


def test_sync_merged_prs_to_done_skips_when_no_done_state() -> None:
    candidate = _candidate()
    fake_linear = _MergeSyncFakeLinear(
        candidates=[candidate],
        comments_by_issue={},
        team_states={},  # no Done state
    )
    github = _StubGithub({})

    report = sync_merged_prs_to_done(
        linear=fake_linear,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        team_id='t1',
    )
    assert report.moved_to_done == []


# --- Phase 6: worktree GC ----------------------------------------------------


from unittest.mock import patch  # noqa: E402

from albedo.housekeeping import gc_worktrees  # noqa: E402
from albedo.linear_client import LinearError  # noqa: E402
from albedo.worktree import WorktreeInfo  # noqa: E402


class _GcFakeLinear:
    def __init__(self, *, issues_by_identifier: dict[str, Issue]) -> None:
        self.issues_by_identifier = issues_by_identifier
        self.lookups: list[str] = []
        self.batch_lookups: list[list[str]] = []

    def get_issue(self, identifier: str) -> Issue:
        self.lookups.append(identifier)
        if identifier not in self.issues_by_identifier:
            raise LinearError(f'unknown {identifier!r}')
        return self.issues_by_identifier[identifier]

    def list_issues_by_identifiers(
        self, identifiers: list[str], *, limit: int = 100
    ) -> list[Issue]:
        del limit
        self.batch_lookups.append(list(identifiers))
        return [
            self.issues_by_identifier[i]
            for i in identifiers
            if i in self.issues_by_identifier
        ]


def _wt(branch: str, path: str = '/tmp/wt/x') -> WorktreeInfo:
    return WorktreeInfo(path=Path(path), branch=branch, base_branch='main')


def _done_issue(identifier: str) -> Issue:
    return Issue(
        id=f'uuid-{identifier}',
        identifier=identifier,
        title='X',
        description='',
        state_id='state-done',
        state_name='Done',
        assignee_id=None,
        label_ids=(),
        label_names=(),
        parent_id=None,
        branch_name='',
    )


def _backlog_issue(identifier: str) -> Issue:
    issue = _done_issue(identifier)
    return Issue(
        id=issue.id,
        identifier=issue.identifier,
        title=issue.title,
        description=issue.description,
        state_id='state-backlog',
        state_name='Backlog',
        assignee_id=None,
        label_ids=(),
        label_names=(),
        parent_id=None,
        branch_name='',
    )


def test_gc_worktrees_removes_done_clean(tmp_path: Path) -> None:
    fake = _GcFakeLinear(issues_by_identifier={'AI-5': _done_issue('AI-5')})
    with (
        patch('albedo.housekeeping.list_worktrees') as mock_list,
        patch('albedo.housekeeping.has_uncommitted_changes', return_value=False),
        patch('albedo.housekeeping.has_unpushed_commits', return_value=False),
        patch('albedo.housekeeping.remove_worktree') as mock_remove,
    ):
        mock_list.return_value = [_wt('task/ai-5', '/tmp/wt/sample-ai-5')]
        report = gc_worktrees(linear=fake, repo_path=tmp_path)  # type: ignore[arg-type]

    assert list(report.removed) == ['/tmp/wt/sample-ai-5']
    assert mock_remove.call_count == 1


def test_gc_worktrees_keeps_when_issue_not_finished(tmp_path: Path) -> None:
    fake = _GcFakeLinear(issues_by_identifier={'AI-5': _backlog_issue('AI-5')})
    with (
        patch('albedo.housekeeping.list_worktrees') as mock_list,
        patch('albedo.housekeeping.has_uncommitted_changes', return_value=False),
        patch('albedo.housekeeping.has_unpushed_commits', return_value=False),
        patch('albedo.housekeeping.remove_worktree') as mock_remove,
    ):
        mock_list.return_value = [_wt('task/ai-5')]
        report = gc_worktrees(linear=fake, repo_path=tmp_path)  # type: ignore[arg-type]

    assert report.removed == []
    assert mock_remove.call_count == 0


def test_gc_worktrees_keeps_when_uncommitted(tmp_path: Path) -> None:
    fake = _GcFakeLinear(issues_by_identifier={'AI-5': _done_issue('AI-5')})
    with (
        patch('albedo.housekeeping.list_worktrees') as mock_list,
        patch('albedo.housekeeping.has_uncommitted_changes', return_value=True),
        patch('albedo.housekeeping.has_unpushed_commits', return_value=False),
        patch('albedo.housekeeping.remove_worktree') as mock_remove,
    ):
        mock_list.return_value = [_wt('task/ai-5')]
        report = gc_worktrees(linear=fake, repo_path=tmp_path)  # type: ignore[arg-type]

    assert report.removed == []
    assert mock_remove.call_count == 0


def test_gc_worktrees_keeps_when_unpushed(tmp_path: Path) -> None:
    fake = _GcFakeLinear(issues_by_identifier={'AI-5': _done_issue('AI-5')})
    with (
        patch('albedo.housekeeping.list_worktrees') as mock_list,
        patch('albedo.housekeeping.has_uncommitted_changes', return_value=False),
        patch('albedo.housekeeping.has_unpushed_commits', return_value=True),
        patch('albedo.housekeeping.remove_worktree') as mock_remove,
    ):
        mock_list.return_value = [_wt('task/ai-5')]
        report = gc_worktrees(linear=fake, repo_path=tmp_path)  # type: ignore[arg-type]

    assert report.removed == []
    assert mock_remove.call_count == 0


def test_gc_worktrees_skips_non_task_branches(tmp_path: Path) -> None:
    fake = _GcFakeLinear(issues_by_identifier={})
    with (
        patch('albedo.housekeeping.list_worktrees') as mock_list,
        patch('albedo.housekeeping.has_uncommitted_changes', return_value=False),
        patch('albedo.housekeeping.has_unpushed_commits', return_value=False),
        patch('albedo.housekeeping.remove_worktree') as mock_remove,
    ):
        mock_list.return_value = [_wt('feature/random')]
        report = gc_worktrees(linear=fake, repo_path=tmp_path)  # type: ignore[arg-type]

    assert report.removed == []
    assert mock_remove.call_count == 0
    assert fake.lookups == []


# --- Phase 7: archive job tests --------------------------------------------


from albedo.housekeeping import archive_old_done_issues  # noqa: E402


class _ArchiveFakeLinear:
    def __init__(self, *, nodes: list[dict[str, Any]]) -> None:
        self._nodes = nodes
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.archived_ids: list[str] = []

    def query(self, document: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.queries.append((document, variables))
        return {'issues': {'nodes': self._nodes}}

    def archive_issue(self, issue_id: str) -> None:
        self.archived_ids.append(issue_id)


def _done_node(identifier: str, completed_at: str) -> dict[str, Any]:
    return {
        'id': f'uuid-{identifier}',
        'identifier': identifier,
        'title': 'x',
        'description': '',
        'state': {'id': 's', 'name': 'Done'},
        'assignee': None,
        'labels': {'nodes': []},
        'parent': None,
        'branchName': '',
        'relations': {'nodes': []},
        'inverseRelations': {'nodes': []},
        'completedAt': completed_at,
    }


def test_archive_disabled_when_zero_days() -> None:
    fake = _ArchiveFakeLinear(nodes=[_done_node('AI-1', '2026-01-01T00:00:00Z')])
    report = archive_old_done_issues(linear=fake, team_id='t', older_than_days=0)  # type: ignore[arg-type]
    assert report.archived == []
    assert fake.queries == []


def test_archive_archives_old_issues_only() -> None:
    fake = _ArchiveFakeLinear(
        nodes=[
            _done_node('AI-1', '2026-04-01T00:00:00+00:00'),
            _done_node('AI-2', '2026-04-25T00:00:00+00:00'),
        ]
    )
    now = datetime(2026, 5, 1, tzinfo=UTC)
    report = archive_old_done_issues(
        linear=fake,  # type: ignore[arg-type]
        team_id='t',
        older_than_days=7,
        now=now,
    )
    assert report.archived == ['AI-1', 'AI-2']
    assert fake.archived_ids == ['uuid-AI-1', 'uuid-AI-2']
    cutoff = (now - timedelta(days=7)).isoformat()
    _, variables = fake.queries[0]
    assert variables['filter']['completedAt']['lt'] == cutoff


def test_archive_skips_nodes_without_completed_at() -> None:
    node = _done_node('AI-1', '2026-04-01T00:00:00+00:00')
    node['completedAt'] = None
    fake = _ArchiveFakeLinear(nodes=[node])
    report = archive_old_done_issues(
        linear=fake,  # type: ignore[arg-type]
        team_id='t',
        older_than_days=7,
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert report.archived == []
    assert fake.archived_ids == []


def test_archive_swallows_query_failure() -> None:
    class _Boom:
        def query(self, *_: Any, **__: Any) -> dict[str, Any]:
            raise RuntimeError('bang')

        def archive_issue(self, *_: Any) -> None:
            raise AssertionError('should not be called')

    report = archive_old_done_issues(
        linear=_Boom(),  # type: ignore[arg-type]
        team_id='t',
        older_than_days=7,
    )
    assert report.archived == []
