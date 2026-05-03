"""Tests for the pure CI-redispatch utilities."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast

import pytest

from albedo.ci_redispatch import (
    LAST_CI_RUN_STATE_FILE,
    LAST_CI_RUN_STATE_VERSION,
    CiRedispatchReport,
    extract_linear_identifier,
    format_ci_failure_comment,
    load_last_ci_run,
    redispatch_on_failed_ci,
    save_last_ci_run,
    seed_first_observation,
    truncate_log_tail,
)
from albedo.github_client import (
    PullRequest,
    WorkflowJob,
    WorkflowRun,
    WorkflowStep,
)
from albedo.linear_client import Issue, IssueUpdate


class TestExtractLinearIdentifier:
    def test_bare_identifier(self) -> None:
        assert extract_linear_identifier('AI-58') == 'AI-58'

    def test_task_prefix(self) -> None:
        assert extract_linear_identifier('task/AI-58') == 'AI-58'

    def test_lowercase_uppercased(self) -> None:
        assert extract_linear_identifier('task/ai-58-add-utils') == 'AI-58'

    def test_mixed_case(self) -> None:
        assert extract_linear_identifier('feature/Ai-12') == 'AI-12'

    def test_no_identifier_returns_none(self) -> None:
        assert extract_linear_identifier('main') is None

    def test_only_digits_returns_none(self) -> None:
        assert extract_linear_identifier('release/2026-05') is None

    def test_only_letters_returns_none(self) -> None:
        assert extract_linear_identifier('feature/foo') is None

    def test_first_match_wins_when_multiple(self) -> None:
        assert extract_linear_identifier('task/AI-58-then-AI-12-fix') == 'AI-58'

    def test_first_match_wins_across_prefixes(self) -> None:
        assert extract_linear_identifier('eng-9/AI-58-thing') == 'ENG-9'

    def test_empty_string(self) -> None:
        assert extract_linear_identifier('') is None

    def test_word_boundary_ignores_substring(self) -> None:
        # `notai-58` does not start at a word boundary for the alpha run,
        # but `\b` still anchors before `notai`, so this DOES match the
        # whole alpha run. The point of the boundary is that `1ai-58`
        # would not match — the alpha cluster has to start at a boundary.
        assert extract_linear_identifier('1ai-58') is None


class TestTruncateLogTail:
    def test_empty_log_returns_empty(self) -> None:
        assert truncate_log_tail('') == ''

    def test_log_shorter_than_max_returned_unchanged(self) -> None:
        log = '\n'.join(f'line {i}' for i in range(50))
        assert truncate_log_tail(log, max_lines=200) == log

    def test_log_at_max_returned_unchanged(self) -> None:
        log = '\n'.join(f'line {i}' for i in range(200))
        assert truncate_log_tail(log, max_lines=200) == log

    def test_log_longer_than_max_prepends_marker(self) -> None:
        log = '\n'.join(f'line {i}' for i in range(250))
        result = truncate_log_tail(log, max_lines=200)
        lines = result.split('\n')
        assert lines[0] == '… (50 earlier lines elided)'
        assert lines[1] == 'line 50'
        assert lines[-1] == 'line 249'
        assert len(lines) == 201

    def test_default_max_lines_is_200(self) -> None:
        log = '\n'.join(f'line {i}' for i in range(205))
        result = truncate_log_tail(log)
        assert result.startswith('… (5 earlier lines elided)')

    def test_single_line_log_unchanged(self) -> None:
        assert truncate_log_tail('only line') == 'only line'

    def test_custom_max_lines(self) -> None:
        log = '\n'.join(f'line {i}' for i in range(10))
        result = truncate_log_tail(log, max_lines=3)
        lines = result.split('\n')
        assert lines[0] == '… (7 earlier lines elided)'
        assert lines[1:] == ['line 7', 'line 8', 'line 9']


class TestFormatCiFailureComment:
    def test_shape(self) -> None:
        body = format_ci_failure_comment(
            workflow_name='CI',
            run_url='https://github.com/o/r/actions/runs/42',
            job_name='unit-tests',
            step_name='pytest',
            log_tail='E   AssertionError\n',
        )
        lines = body.split('\n')
        assert lines[0].startswith('**CI failure**')
        assert 'CI' in lines[0]
        assert 'https://github.com/o/r/actions/runs/42' in lines[0]
        assert lines[1].startswith('Failed job:')
        assert '`unit-tests`' in lines[1]
        assert '`pytest`' in lines[1]
        assert '```log' in body
        assert 'E   AssertionError' in body
        assert body.rstrip().endswith('```')

    def test_log_block_wraps_verbatim(self) -> None:
        body = format_ci_failure_comment(
            workflow_name='ci.yml',
            run_url='https://x',
            job_name='j',
            step_name='s',
            log_tail='line-a\nline-b',
        )
        assert '```log\nline-a\nline-b\n```' in body

    def test_empty_log_tail_still_renders_fence(self) -> None:
        body = format_ci_failure_comment(
            workflow_name='ci',
            run_url='https://x',
            job_name='j',
            step_name='s',
            log_tail='',
        )
        assert '```log\n\n```' in body


class TestLoadSaveLastCiRun:
    def test_load_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_last_ci_run(tmp_path) == {}

    def test_round_trip(self, tmp_path: Path) -> None:
        mapping = {'uuid-a': 'run-1', 'uuid-b': 'run-2'}
        save_last_ci_run(tmp_path, mapping)
        loaded = load_last_ci_run(tmp_path)
        assert loaded == mapping

    def test_save_creates_state_file_with_version_key(self, tmp_path: Path) -> None:
        save_last_ci_run(tmp_path, {'uuid-a': 'run-1'})
        path = tmp_path / LAST_CI_RUN_STATE_FILE
        assert path.exists()
        payload = json.loads(path.read_text(encoding='utf-8'))
        assert payload['version'] == LAST_CI_RUN_STATE_VERSION
        assert payload['runs'] == {'uuid-a': 'run-1'}

    def test_save_creates_parent_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / 'state' / 'sub'
        save_last_ci_run(nested, {'uuid-a': 'run-1'})
        assert (nested / LAST_CI_RUN_STATE_FILE).exists()

    def test_repeated_saves_idempotent(self, tmp_path: Path) -> None:
        mapping = {'uuid-a': 'run-1', 'uuid-b': 'run-2'}
        save_last_ci_run(tmp_path, mapping)
        first = (tmp_path / LAST_CI_RUN_STATE_FILE).read_bytes()
        save_last_ci_run(tmp_path, mapping)
        second = (tmp_path / LAST_CI_RUN_STATE_FILE).read_bytes()
        assert first == second

    def test_save_order_independent(self, tmp_path: Path) -> None:
        save_last_ci_run(tmp_path, {'b': '2', 'a': '1'})
        first = (tmp_path / LAST_CI_RUN_STATE_FILE).read_bytes()
        save_last_ci_run(tmp_path, {'a': '1', 'b': '2'})
        second = (tmp_path / LAST_CI_RUN_STATE_FILE).read_bytes()
        assert first == second

    def test_corrupt_json_returns_empty_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / LAST_CI_RUN_STATE_FILE
        path.write_text('{not valid json', encoding='utf-8')
        with caplog.at_level(logging.WARNING, logger='albedo.ci_redispatch'):
            assert load_last_ci_run(tmp_path) == {}
        assert any('corrupt' in rec.message for rec in caplog.records)

    def test_wrong_version_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / LAST_CI_RUN_STATE_FILE
        path.write_text(
            json.dumps({'version': 999, 'runs': {'a': '1'}}), encoding='utf-8'
        )
        assert load_last_ci_run(tmp_path) == {}

    def test_non_dict_payload_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / LAST_CI_RUN_STATE_FILE
        path.write_text('[1, 2, 3]', encoding='utf-8')
        assert load_last_ci_run(tmp_path) == {}

    def test_runs_not_a_dict_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / LAST_CI_RUN_STATE_FILE
        path.write_text(
            json.dumps({'version': LAST_CI_RUN_STATE_VERSION, 'runs': []}),
            encoding='utf-8',
        )
        assert load_last_ci_run(tmp_path) == {}

    def test_save_then_overwrite_replaces_mapping(self, tmp_path: Path) -> None:
        save_last_ci_run(tmp_path, {'a': '1'})
        save_last_ci_run(tmp_path, {'a': '2', 'b': '3'})
        assert load_last_ci_run(tmp_path) == {'a': '2', 'b': '3'}


class TestSeedFirstObservation:
    def test_seeds_when_unseen(self) -> None:
        state: dict[str, str] = {}
        seeded = seed_first_observation(state, 'uuid-a', 'run-1')
        assert seeded is True
        assert state == {'uuid-a': 'run-1'}

    def test_does_not_seed_when_already_known(self) -> None:
        state = {'uuid-a': 'run-1'}
        seeded = seed_first_observation(state, 'uuid-a', 'run-2')
        assert seeded is False
        assert state == {'uuid-a': 'run-1'}

    def test_seeds_per_issue_independently(self) -> None:
        state: dict[str, str] = {'uuid-a': 'run-1'}
        seeded = seed_first_observation(state, 'uuid-b', 'run-9')
        assert seeded is True
        assert state == {'uuid-a': 'run-1', 'uuid-b': 'run-9'}


def _issue(
    *,
    identifier: str = 'AI-58',
    issue_id: str = 'uuid-58',
    label_names: tuple[str, ...] = ('kind:final-pr',),
    label_ids: tuple[str, ...] = ('lab-final',),
    state_name: str = 'Awaiting approval',
) -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title='X',
        description='',
        state_id='state-await',
        state_name=state_name,
        assignee_id=None,
        label_ids=label_ids,
        label_names=label_names,
        parent_id=None,
        branch_name='',
    )


class _FakeLinear:
    """Minimal LinearClient surface used by redispatch_on_failed_ci."""

    def __init__(
        self,
        *,
        issues: list[Issue] | None = None,
    ) -> None:
        self.issues = issues or []
        self.team_states: dict[str, str] = {
            'In Progress': 'state-in-progress',
            'Awaiting approval': 'state-await',
            'Backlog': 'state-backlog',
        }
        self.team_labels: dict[str, str] = {
            'kind:final-pr': 'lab-final',
            'stuck': 'lab-stuck',
            'blocked-external': 'lab-blocked-ext',
            'awaiting-human-reply': 'lab-await-human',
        }
        self.updates: list[tuple[str, IssueUpdate]] = []
        self.comments: list[tuple[str, str]] = []

    def add_comment(self, issue_id: str, body: str) -> str:
        self.comments.append((issue_id, body))
        return f'comment-{len(self.comments)}'

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
            'parent': None,
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
        if 'CiAwaitingApproval' in document:
            vars_dict = cast('dict[str, Any]', variables) if variables else {}
            filt = cast('dict[str, Any]', vars_dict.get('filter', {}))
            state_obj = cast('dict[str, Any]', filt.get('state', {}))
            name_obj = cast('dict[str, Any]', state_obj.get('name', {}))
            wanted = name_obj.get('eq')
            picked = [i for i in self.issues if i.state_name == wanted]
            return {'issues': {'nodes': [self._issue_node(i) for i in picked]}}
        if 'StateByName' in document:
            return {
                'workflowStates': {
                    'nodes': [
                        {'id': sid, 'name': name}
                        for name, sid in self.team_states.items()
                    ]
                }
            }
        if 'LabelsForIssue' in document:
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
        raise AssertionError(f'unexpected query: {document!r}')


class _FakeGithub:
    """Minimal GithubClient surface used by redispatch_on_failed_ci."""

    def __init__(
        self,
        *,
        pulls: list[PullRequest] | None = None,
        runs: dict[str, list[WorkflowRun]] | None = None,
        jobs: dict[int, list[WorkflowJob]] | None = None,
        logs: dict[int, str] | None = None,
    ) -> None:
        self.pulls = pulls or []
        self.runs = runs or {}
        self.jobs = jobs or {}
        self.logs = logs or {}
        self.list_pulls_calls = 0
        self.list_runs_calls: list[str] = []

    def list_pull_requests(
        self, owner: str, repo: str, *, state: str = 'open'
    ) -> list[PullRequest]:
        del owner, repo, state
        self.list_pulls_calls += 1
        return list(self.pulls)

    def list_workflow_runs(
        self, owner: str, repo: str, head_branch: str
    ) -> list[WorkflowRun]:
        del owner, repo
        self.list_runs_calls.append(head_branch)
        return list(self.runs.get(head_branch, []))

    def list_workflow_jobs(
        self, owner: str, repo: str, run_id: int
    ) -> list[WorkflowJob]:
        del owner, repo
        return list(self.jobs.get(run_id, []))

    def get_job_logs(self, owner: str, repo: str, job_id: int) -> str:
        del owner, repo
        return self.logs.get(job_id, '')


def _failed_run(
    run_id: int = 999, branch_url: str = 'https://github.com/o/r/actions/runs/999'
) -> WorkflowRun:
    return WorkflowRun(
        id=run_id,
        name='CI',
        status='completed',
        conclusion='failure',
        html_url=branch_url,
        head_sha='deadbee',
    )


def _success_run(run_id: int = 100) -> WorkflowRun:
    return WorkflowRun(
        id=run_id,
        name='CI',
        status='completed',
        conclusion='success',
        html_url=f'https://github.com/o/r/actions/runs/{run_id}',
        head_sha='cafe',
    )


def _in_progress_run(run_id: int = 50) -> WorkflowRun:
    return WorkflowRun(
        id=run_id,
        name='CI',
        status='in_progress',
        conclusion=None,
        html_url=f'https://github.com/o/r/actions/runs/{run_id}',
        head_sha='abc',
    )


def _failing_job_with_step(
    job_id: int = 7,
    job_name: str = 'tests',
    step_name: str = 'pytest',
) -> WorkflowJob:
    return WorkflowJob(
        id=job_id,
        name=job_name,
        conclusion='failure',
        steps=(
            WorkflowStep(name='Setup', number=1, conclusion='success'),
            WorkflowStep(name=step_name, number=2, conclusion='failure'),
        ),
    )


def _open_pr(branch: str, number: int = 1) -> PullRequest:
    return PullRequest(
        owner='o',
        repo='r',
        number=number,
        state='open',
        merged=False,
        head_branch=branch,
    )


def _seed_state(tmp_path: Path, mapping: dict[str, str]) -> None:
    save_last_ci_run(tmp_path, mapping)


def _redispatch(
    tmp_path: Path,
    *,
    linear: _FakeLinear,
    github: _FakeGithub,
) -> CiRedispatchReport:
    return redispatch_on_failed_ci(
        linear=linear,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        state_dir=tmp_path,
        repo_owner='o',
        repo_name='r',
        team_id='team-1',
    )


class TestRedispatchOnFailedCi:
    def test_no_candidates_no_op(self, tmp_path: Path) -> None:
        linear = _FakeLinear(issues=[])
        github = _FakeGithub()
        report = _redispatch(tmp_path, linear=linear, github=github)
        assert report.inspected == 0  # type: ignore[attr-defined]
        assert github.list_pulls_calls == 0
        assert linear.updates == []

    def test_priming_on_first_observation(self, tmp_path: Path) -> None:
        issue = _issue()
        linear = _FakeLinear(issues=[issue])
        github = _FakeGithub(
            pulls=[_open_pr('task/AI-58')],
            runs={'task/AI-58': [_failed_run(run_id=42)]},
        )
        report = _redispatch(tmp_path, linear=linear, github=github)
        assert linear.comments == []
        assert linear.updates == []
        assert report.seeded == ('AI-58',)  # type: ignore[attr-defined]
        # State file pinned to the observed run id.
        assert load_last_ci_run(tmp_path) == {issue.id: '42'}

    def test_skip_when_already_reported(self, tmp_path: Path) -> None:
        issue = _issue()
        _seed_state(tmp_path, {issue.id: '42'})
        linear = _FakeLinear(issues=[issue])
        github = _FakeGithub(
            pulls=[_open_pr('task/AI-58')],
            runs={'task/AI-58': [_failed_run(run_id=42)]},
        )
        report = _redispatch(tmp_path, linear=linear, github=github)
        assert linear.comments == []
        assert linear.updates == []
        assert report.fired == ()  # type: ignore[attr-defined]

    def test_skip_when_in_progress(self, tmp_path: Path) -> None:
        issue = _issue()
        _seed_state(tmp_path, {issue.id: '7'})
        linear = _FakeLinear(issues=[issue])
        github = _FakeGithub(
            pulls=[_open_pr('task/AI-58')],
            runs={'task/AI-58': [_in_progress_run(run_id=99)]},
        )
        report = _redispatch(tmp_path, linear=linear, github=github)
        assert linear.comments == []
        assert linear.updates == []
        assert report.fired == ()  # type: ignore[attr-defined]
        # Seed unchanged — in-progress runs don't update state.
        assert load_last_ci_run(tmp_path) == {issue.id: '7'}

    def test_skip_when_latest_in_progress_shadows_older_completed(
        self, tmp_path: Path
    ) -> None:
        # GitHub returns runs newest-first. If the most recent run is
        # still running, we wait for it instead of acting on a stale
        # completed run below it.
        issue = _issue()
        _seed_state(tmp_path, {issue.id: '7'})
        linear = _FakeLinear(issues=[issue])
        github = _FakeGithub(
            pulls=[_open_pr('task/AI-58')],
            runs={
                'task/AI-58': [
                    _in_progress_run(run_id=99),
                    _failed_run(run_id=42),
                ]
            },
        )
        report = _redispatch(tmp_path, linear=linear, github=github)
        assert linear.comments == []
        assert linear.updates == []
        assert report.fired == ()  # type: ignore[attr-defined]
        # State unchanged — neither run was acted on.
        assert load_last_ci_run(tmp_path) == {issue.id: '7'}

    def test_skip_when_no_pr_matches_branch(self, tmp_path: Path) -> None:
        issue = _issue()
        _seed_state(tmp_path, {issue.id: '7'})
        linear = _FakeLinear(issues=[issue])
        # Open PRs exist but none map to AI-58 — analogous to the "PR
        # merged / closed-without-merge" case (the merged PR is no
        # longer in the open-PR listing).
        github = _FakeGithub(
            pulls=[_open_pr('task/AI-99')],
            runs={'task/AI-99': [_failed_run(run_id=42)]},
        )
        report = _redispatch(tmp_path, linear=linear, github=github)
        assert linear.comments == []
        assert linear.updates == []
        assert report.fired == ()  # type: ignore[attr-defined]
        # Negative-path AC: state untouched when no PR matches.
        assert load_last_ci_run(tmp_path) == {issue.id: '7'}
        # Workflow-runs lookup never happened — no PR to look up by.
        assert github.list_runs_calls == []

    def test_skip_when_merged(self, tmp_path: Path) -> None:
        # Same code path as "no PR matches": a merged PR has dropped
        # out of the open-PR listing, so the cache misses.
        issue = _issue()
        _seed_state(tmp_path, {issue.id: '7'})
        linear = _FakeLinear(issues=[issue])
        github = _FakeGithub(pulls=[])
        report = _redispatch(tmp_path, linear=linear, github=github)
        assert linear.comments == []
        assert linear.updates == []
        assert report.fired == ()  # type: ignore[attr-defined]

    def test_skip_when_latest_run_is_success(self, tmp_path: Path) -> None:
        issue = _issue()
        _seed_state(tmp_path, {issue.id: '7'})
        linear = _FakeLinear(issues=[issue])
        github = _FakeGithub(
            pulls=[_open_pr('task/AI-58')],
            runs={'task/AI-58': [_success_run(run_id=42)]},
        )
        report = _redispatch(tmp_path, linear=linear, github=github)
        assert linear.comments == []
        assert linear.updates == []
        assert report.fired == ()  # type: ignore[attr-defined]
        # Successful run id pinned so we don't replay if it later flips.
        assert load_last_ci_run(tmp_path) == {issue.id: '42'}

    def test_fire_on_fresh_failure(self, tmp_path: Path) -> None:
        issue = _issue(
            label_names=('kind:final-pr', 'awaiting-human-reply', 'stuck'),
            label_ids=('lab-final', 'lab-await-human', 'lab-stuck'),
        )
        # Seed with prior run so the new failure is "fresh", not first
        # observation (priming).
        _seed_state(tmp_path, {issue.id: '7'})
        linear = _FakeLinear(issues=[issue])
        log_text = 'line 0\nline 1\nE   AssertionError: boom\n'
        github = _FakeGithub(
            pulls=[_open_pr('task/AI-58')],
            runs={
                'task/AI-58': [
                    _failed_run(
                        run_id=42,
                        branch_url='https://github.com/o/r/actions/runs/42',
                    )
                ]
            },
            jobs={42: [_failing_job_with_step(job_id=7)]},
            logs={7: log_text},
        )
        report = _redispatch(tmp_path, linear=linear, github=github)

        assert len(report.fired) == 1  # type: ignore[attr-defined]
        result = report.fired[0]  # type: ignore[attr-defined]
        assert result.issue_identifier == 'AI-58'
        assert result.run_id == 42

        assert len(linear.comments) == 1
        comment_issue_id, body = linear.comments[0]
        assert comment_issue_id == issue.id
        assert '**CI failure**' in body
        assert 'https://github.com/o/r/actions/runs/42' in body
        assert '`tests`' in body
        assert '`pytest`' in body
        assert 'AssertionError: boom' in body

        assert len(linear.updates) == 1
        update_id, update = linear.updates[0]
        assert update_id == issue.id
        assert update.state_id == 'state-in-progress'
        assert update.unset_assignee is True
        # Pickup-blocker labels stripped; non-blocker label kept.
        assert update.label_ids is not None
        assert 'lab-await-human' not in update.label_ids
        assert 'lab-stuck' not in update.label_ids
        assert 'lab-final' in update.label_ids

        # State persisted with the new run id.
        assert load_last_ci_run(tmp_path) == {issue.id: '42'}

    def test_pr_cache_hit_across_issues(self, tmp_path: Path) -> None:
        issue_a = _issue(identifier='AI-58', issue_id='uuid-58')
        issue_b = _issue(identifier='AI-59', issue_id='uuid-59')
        _seed_state(tmp_path, {issue_a.id: '1', issue_b.id: '2'})
        linear = _FakeLinear(issues=[issue_a, issue_b])
        github = _FakeGithub(
            pulls=[_open_pr('task/AI-58', number=1), _open_pr('task/AI-59', number=2)],
            # Both branches still in progress so we don't fire — focus
            # the test on cache reuse, not the firing path.
            runs={
                'task/AI-58': [_in_progress_run(run_id=10)],
                'task/AI-59': [_in_progress_run(run_id=11)],
            },
        )
        _redispatch(tmp_path, linear=linear, github=github)
        # PR list fetched exactly once even though two issues consulted it.
        assert github.list_pulls_calls == 1
        # Workflow runs queried per-branch (one per issue).
        assert sorted(github.list_runs_calls) == ['task/AI-58', 'task/AI-59']

    def test_pr_cache_miss_skips_silently(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        issue = _issue(identifier='AI-77', issue_id='uuid-77')
        _seed_state(tmp_path, {issue.id: '7'})
        linear = _FakeLinear(issues=[issue])
        github = _FakeGithub(
            pulls=[_open_pr('task/AI-58')],
            runs={'task/AI-58': [_failed_run(run_id=42)]},
        )
        with caplog.at_level(logging.DEBUG, logger='albedo.ci_redispatch'):
            _redispatch(tmp_path, linear=linear, github=github)
        assert linear.updates == []
        assert linear.comments == []
        assert any('AI-77' in rec.message for rec in caplog.records)
        # PR list still fetched, but workflow-runs never consulted.
        assert github.list_pulls_calls == 1
        assert github.list_runs_calls == []
