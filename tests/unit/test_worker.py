"""Tests for worker.run_once and helpers."""

from __future__ import annotations

import multiprocessing as mp
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from pydantic import SecretStr

from albedo import worker as worker_mod
from albedo.claude_runner import ClaudeRunResult
from albedo.config import LinearConfig, OrchestratorConfig, RepoConfig
from albedo.dispatch_messages import (
    CandidateMsg,
    ClaimedOk,
    ClaimLost,
)
from albedo.linear_client import IncomingRelation, Issue, IssueUpdate
from albedo.worker import (
    acceptance_criteria_from_description,
    attempts_from_labels,
    build_mcp_extra_env,
    filter_dispatchable,
    parse_pr_url,
    run_once,
)
from tests._prompts_dir import bundled_prompts_dir


def test_attempts_from_labels_picks_max() -> None:
    assert attempts_from_labels(()) == 0
    assert attempts_from_labels(('attempts:0',)) == 0
    assert attempts_from_labels(('attempts:1', 'attempts:3', 'size:M')) == 3
    assert attempts_from_labels(('size:M',)) == 0


def test_acceptance_criteria_extracts_bullets_after_header() -> None:
    description = (
        'Some intro.\n\n'
        '## Acceptance Criteria\n'
        '- /filter endpoint returns 200\n'
        '- [x] Tests cover empty input\n'
        '* Old behaviour still works\n'
        '\n'
        '## Notes\n'
        '- not part of AC\n'
    )
    assert acceptance_criteria_from_description(description) == (
        '/filter endpoint returns 200',
        'Tests cover empty input',
        'Old behaviour still works',
    )


def test_acceptance_criteria_returns_empty_when_no_header() -> None:
    assert acceptance_criteria_from_description('just description') == ()


def test_acceptance_criteria_handles_numbered_list() -> None:
    description = '## Acceptance criteria\n1. First\n2. Second\n'
    assert acceptance_criteria_from_description(description) == ('First', 'Second')


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if not shutil.which('git'):
        pytest.skip('git is not available')
    upstream = tmp_path / 'upstream.git'
    workdir = tmp_path / 'sample-repo'
    subprocess.run(['git', 'init', '--bare', str(upstream)], check=True)
    subprocess.run(
        ['git', 'clone', str(upstream), str(workdir)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ['git', '-C', str(workdir), 'config', 'user.email', 'test@example.com'],
        check=True,
    )
    subprocess.run(
        ['git', '-C', str(workdir), 'config', 'user.name', 'Test'],
        check=True,
    )
    (workdir / 'README.md').write_text('hello\n', encoding='utf-8')
    subprocess.run(['git', '-C', str(workdir), 'add', '.'], check=True)
    subprocess.run(
        ['git', '-C', str(workdir), 'commit', '-m', 'init'],
        check=True,
        capture_output=True,
    )
    subprocess.run(['git', '-C', str(workdir), 'branch', '-M', 'main'], check=True)
    subprocess.run(
        ['git', '-C', str(workdir), 'push', '-u', 'origin', 'main'],
        check=True,
        capture_output=True,
    )
    return workdir


class _FakeLinear:
    def __init__(self, issue: Issue) -> None:
        self._issue = issue
        self.comments: list[tuple[str, str]] = []
        self.updates: list[tuple[str, object]] = []
        self.team_states: dict[str, str] = {
            'Backlog': 'state-backlog',
            'Review': 'state-review',
            'Awaiting approval': 'state-await',
        }
        self.team_labels: dict[str, str] = {
            'attempts:1': 'lab-att-1',
            'attempts:2': 'lab-att-2',
            'attempts:3': 'lab-att-3',
            'kind:final-pr': 'lab-final',
            'stuck': 'lab-stuck',
            'draft': 'lab-draft',
            'awaiting-human-reply': 'lab-await-human',
        }
        self.linear_comments: list[object] = []

    def get_issue(self, identifier: str) -> Issue:
        assert identifier == self._issue.identifier
        return self._issue

    def query(self, document: str, _variables: object) -> dict[str, object]:
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
            'issue': {
                'team': {
                    'states': {
                        'nodes': [
                            {'id': sid, 'name': name}
                            for name, sid in self.team_states.items()
                        ]
                    }
                }
            }
        }

    def list_comments(self, _issue_id: str) -> list[object]:
        return list(self.linear_comments)

    def add_comment(self, issue_id: str, body: str) -> str:
        self.comments.append((issue_id, body))
        return 'comment-stub'

    def update_issue(self, issue_id: str, update: object) -> Issue:
        self.updates.append((issue_id, update))
        return self._issue


def _issue() -> Issue:
    return Issue(
        id='uuid-1',
        identifier='AI-5',
        title='Add filter',
        description='## Acceptance Criteria\n- Filter works\n',
        state_id='state-backlog',
        state_name='Backlog',
        assignee_id=None,
        label_ids=('lab-1',),
        label_names=('attempts:1',),
        parent_id=None,
        branch_name='roman/ai-5',
    )


def test_build_mcp_extra_env_prefers_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('GITHUB_PERSONAL_ACCESS_TOKEN', 'ghp_from_shell')
    env = build_mcp_extra_env(SecretStr('ghp_from_dotenv'))
    assert env == {'GITHUB_PERSONAL_ACCESS_TOKEN': 'ghp_from_shell'}


def test_build_mcp_extra_env_falls_back_to_dotenv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv('GITHUB_PERSONAL_ACCESS_TOKEN', raising=False)
    env = build_mcp_extra_env(SecretStr('ghp_from_dotenv'))
    assert env == {'GITHUB_PERSONAL_ACCESS_TOKEN': 'ghp_from_dotenv'}


def test_build_mcp_extra_env_returns_empty_when_no_pat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv('GITHUB_PERSONAL_ACCESS_TOKEN', raising=False)
    assert build_mcp_extra_env(None) == {}
    assert build_mcp_extra_env(SecretStr('   ')) == {}


class _LoopFakeLinear:
    """Fake Linear client implementing only what `run_loop` touches."""

    def __init__(
        self,
        *,
        team_resolve: object,
        candidates_per_call: list[list[Issue]] | None = None,
        claim_result: bool = True,
    ) -> None:
        self._team = team_resolve
        self._candidates = candidates_per_call or []
        self._call_idx = 0
        self.calls: list[str] = []
        self.updates: list[tuple[str, IssueUpdate]] = []
        self.comments: list[tuple[str, str]] = []
        self._claim_result = claim_result

    def resolve_team(self, _identifier: str) -> object:
        self.calls.append('resolve_team')
        return self._team

    def list_pickup_issues(
        self,
        _team_id: str,
        _states: list[str],
        *,
        exclude_labels: list[str] | None = None,
        project_id: str | None = None,
    ) -> list[Issue]:
        del exclude_labels, project_id
        idx = min(self._call_idx, len(self._candidates) - 1) if self._candidates else -1
        self._call_idx += 1
        return list(self._candidates[idx]) if idx >= 0 else []

    def update_issue(self, issue_id: str, update: IssueUpdate) -> Issue:
        self.updates.append((issue_id, update))
        # Echo back the issue with assignee_id reflecting the update.
        return Issue(
            id=issue_id,
            identifier='AI-5',
            title='X',
            description='',
            state_id='s',
            state_name='Backlog',
            assignee_id=None if update.unset_assignee else update.assignee_id,
            label_ids=(),
            label_names=(),
            parent_id=None,
            branch_name='',
        )

    def add_comment(self, issue_id: str, body: str) -> str:
        self.comments.append((issue_id, body))
        return 'c'

    def query(self, _doc: str, _vars: object) -> dict[str, object]:
        return {
            'issue': {
                'team': {
                    'states': {
                        'nodes': [
                            {'id': 'state-review', 'name': 'Review'},
                            {'id': 'state-backlog', 'name': 'Backlog'},
                        ]
                    }
                }
            }
        }


def test_run_loop_consumes_candidate_runs_claude_and_posts_results(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from albedo.linear_client import Team

    issue = _issue()
    fake = _LoopFakeLinear(team_resolve=Team(id='t1', key='ORC', name='AI'))
    cfg = OrchestratorConfig(
        workers=1,
        project_name='sample',
        poll_interval_seconds=5,
        poll_jitter_seconds=0,
        repo=RepoConfig(path=repo, base_branch='main'),
        linear=LinearConfig(team='ORC'),
        worktree_root=tmp_path / 'wt',
        state_dir=tmp_path / 'state',
    )

    def fake_try_claim(**kwargs: object) -> object:
        from albedo.claim import ClaimResult

        return ClaimResult(
            issue=cast('Issue', kwargs['issue']),
            branch='task/ai-5',
        )

    def fake_spawn(prompt: str, **kwargs: object) -> ClaudeRunResult:
        del prompt, kwargs
        return ClaudeRunResult(
            is_error=False,
            exit_code=0,
            result_text='PR: https://github.com/me/sample/pull/1',
            total_cost_usd=0.0,
            usage={},
        )

    monkeypatch.setattr(worker_mod, 'try_claim', fake_try_claim)
    monkeypatch.setattr(worker_mod, 'spawn_claude', fake_spawn)

    dispatch_queue: mp.Queue = mp.Queue()
    result_queue: mp.Queue = mp.Queue()
    dispatch_queue.put(CandidateMsg(issue=issue, offered_at_unix=0.0))
    dispatch_queue.put(None)  # shutdown sentinel

    worker_mod.run_loop(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        agent_id='1',
        agent_user_id='agent-user',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        dispatch_queue=dispatch_queue,
        result_queue=result_queue,
        install_signal_handlers=False,
    )

    assert any(c[1].startswith('**agent-1**: PR:') for c in fake.comments)
    results: list[object] = []
    while not result_queue.empty():
        results.append(result_queue.get_nowait())
    types = [type(r).__name__ for r in results]
    assert 'ClaimedOk' in types
    assert 'TaskDone' in types


def test_run_loop_posts_claim_lost_when_try_claim_returns_none(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from albedo.linear_client import Team

    issue = _issue()
    fake = _LoopFakeLinear(team_resolve=Team(id='t1', key='ORC', name='AI'))
    cfg = OrchestratorConfig(
        workers=1,
        project_name='sample',
        poll_interval_seconds=5,
        poll_jitter_seconds=0,
        repo=RepoConfig(path=repo, base_branch='main'),
        linear=LinearConfig(team='ORC'),
        worktree_root=tmp_path / 'wt',
        state_dir=tmp_path / 'state',
    )

    monkeypatch.setattr(worker_mod, 'try_claim', lambda **_: None)

    dispatch_queue: mp.Queue = mp.Queue()
    result_queue: mp.Queue = mp.Queue()
    dispatch_queue.put(CandidateMsg(issue=issue, offered_at_unix=0.0))
    dispatch_queue.put(None)

    worker_mod.run_loop(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        agent_id='1',
        agent_user_id='agent-user',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        dispatch_queue=dispatch_queue,
        result_queue=result_queue,
        install_signal_handlers=False,
    )

    msgs: list[object] = []
    while not result_queue.empty():
        msgs.append(result_queue.get_nowait())
    assert any(isinstance(m, ClaimLost) for m in msgs)
    assert not any(isinstance(m, ClaimedOk) for m in msgs)


def _make_issue(
    *,
    state_name: str = 'Backlog',
    parent_id: str | None = None,
    incoming: tuple[IncomingRelation, ...] = (),
) -> Issue:
    return Issue(
        id=f'id-{state_name}-{parent_id}',
        identifier='AI-99',
        title='X',
        description='',
        state_id='s',
        state_name=state_name,
        assignee_id=None,
        label_ids=(),
        label_names=(),
        parent_id=parent_id,
        branch_name='',
        incoming_relations=incoming,
    )


def test_filter_dispatchable_drops_triage_children() -> None:
    triage_child = _make_issue(state_name='Triage', parent_id='parent-1')
    triage_orphan = _make_issue(state_name='Triage', parent_id=None)
    backlog_child = _make_issue(state_name='Backlog', parent_id='parent-1')

    out = filter_dispatchable([triage_child, triage_orphan, backlog_child])

    assert triage_child not in out
    assert triage_orphan in out
    assert backlog_child in out


def test_filter_dispatchable_drops_blocked_by_incomplete() -> None:
    open_blocker = IncomingRelation(type='blocks', source_state_type='started')
    finished_blocker = IncomingRelation(type='blocks', source_state_type='completed')
    blocked = _make_issue(state_name='Backlog', incoming=(open_blocker,))
    unblocked = _make_issue(state_name='Backlog', incoming=(finished_blocker,))
    no_relation = _make_issue(state_name='Backlog')

    out = filter_dispatchable([blocked, unblocked, no_relation])

    assert blocked not in out
    assert unblocked in out
    assert no_relation in out


def test_filter_dispatchable_passes_through_when_clean() -> None:
    issues = [_make_issue(state_name='Backlog'), _make_issue(state_name='Review')]
    assert filter_dispatchable(issues) == issues


def test_parse_pr_url_finds_marker() -> None:
    assert parse_pr_url('PR: https://github.com/me/sample/pull/3') == (
        'https://github.com/me/sample/pull/3'
    )
    assert (
        parse_pr_url('all good\n\nPR: https://github.com/me/sample/pull/42\n')
        == 'https://github.com/me/sample/pull/42'
    )
    assert parse_pr_url('no marker here') is None
    assert parse_pr_url('PR: not a url') is None


def _build_cfg(
    repo: Path,
    tmp_path: Path,
    *,
    max_attempts_before_escalation: int = 3,
) -> OrchestratorConfig:
    return OrchestratorConfig(
        workers=1,
        project_name='sample',
        repo=RepoConfig(path=repo, base_branch='main'),
        linear=LinearConfig(team='ORC'),
        worktree_root=tmp_path / 'wt',
        state_dir=tmp_path / 'state',
        max_attempts_before_escalation=max_attempts_before_escalation,
    )


def _stub_spawn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    captured: list[Mapping[str, object]] | None = None,
    is_error: bool = False,
    result_text: str = 'PR: https://github.com/me/sample/pull/3',
) -> None:
    def fake_spawn(prompt: str, **kwargs: object) -> ClaudeRunResult:
        if captured is not None:
            captured.append({'prompt': prompt, 'kwargs': kwargs})
        return ClaudeRunResult(
            is_error=is_error,
            exit_code=1 if is_error else 0,
            result_text=result_text,
            total_cost_usd=0.0,
            usage={'input_tokens': 10},
        )

    monkeypatch.setattr(worker_mod, 'spawn_claude', fake_spawn)


def test_run_once_happy_path_moves_to_review_and_comments(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Mapping[str, object]] = []
    _stub_spawn(monkeypatch, captured=captured)
    cfg = _build_cfg(repo, tmp_path)
    fake = _FakeLinear(_issue())

    result = run_once(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue_identifier='AI-5',
        agent_id='1',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        cli='claude',
        fetch=False,
    )

    assert result.pr_url == 'https://github.com/me/sample/pull/3'
    assert result.linear_updated is True
    assert fake.comments == [
        ('uuid-1', '**agent-1**: PR: https://github.com/me/sample/pull/3'),
    ]
    assert len(fake.updates) == 1
    update_target_id, update = fake.updates[0]
    assert update_target_id == 'uuid-1'
    assert getattr(update, 'state_id', None) == 'state-review'

    prompt_text = cast('str', captured[0]['prompt'])
    assert 'AI-5' in prompt_text
    assert 'CODER' in prompt_text
    assert 'Filter works' in prompt_text


def test_run_once_blocks_when_no_pr_url(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_spawn(monkeypatch, result_text='BLOCKED: tests failing')
    cfg = _build_cfg(repo, tmp_path)
    fake = _FakeLinear(_issue())

    result = run_once(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue_identifier='AI-5',
        agent_id='1',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        fetch=False,
    )

    assert result.pr_url is None
    assert result.linear_updated is True
    assert fake.comments[0][1].startswith('**agent-1**: BLOCKED')
    _, update = fake.updates[0]
    assert getattr(update, 'state_id', None) == 'state-backlog'
    # Coder asked a clarifying question (`BLOCKED:` line in output) —
    # gate the issue so the worker pool stops re-picking it.
    label_ids = getattr(update, 'label_ids', ()) or ()
    assert 'lab-await-human' in label_ids


def test_run_once_blocks_when_claude_errors(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_spawn(
        monkeypatch,
        is_error=True,
        result_text='PR: https://github.com/me/sample/pull/3',
    )
    cfg = _build_cfg(repo, tmp_path)
    fake = _FakeLinear(_issue())

    result = run_once(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue_identifier='AI-5',
        agent_id='1',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        fetch=False,
    )

    assert result.linear_updated is True
    assert fake.comments[0][1].startswith(
        '**agent-1**: BLOCKED: claude reported an error'
    )
    _, update = fake.updates[0]
    assert getattr(update, 'state_id', None) == 'state-backlog'
    # Pure claude error without a `BLOCKED:` line in output: keep the
    # issue re-pickable so transient failures self-heal, no human gate.
    label_ids = getattr(update, 'label_ids', ()) or ()
    assert 'lab-await-human' not in label_ids


def test_run_once_skips_state_move_when_target_state_missing(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_spawn(monkeypatch)
    cfg = _build_cfg(repo, tmp_path)
    fake = _FakeLinear(_issue())
    fake.team_states = {'Backlog': 'state-backlog'}  # 'Review' missing

    result = run_once(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue_identifier='AI-5',
        agent_id='1',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        fetch=False,
    )

    assert result.linear_updated is False
    assert fake.updates == []
    assert fake.comments[0][1].startswith('**agent-1**: PR:')


def test_run_once_blocker_skips_state_move_when_target_missing(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_spawn(monkeypatch, result_text='no marker')
    cfg = _build_cfg(repo, tmp_path)
    fake = _FakeLinear(_issue())
    fake.team_states = {'Review': 'state-review'}  # 'Backlog' missing

    result = run_once(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue_identifier='AI-5',
        agent_id='1',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        fetch=False,
    )

    assert result.linear_updated is False
    assert fake.updates == []
    assert fake.comments[0][1].startswith('**agent-1**: BLOCKED')


# --- Phase 4: REVIEWER post-spawn -------------------------------------------


def _review_issue(
    *,
    state_name: str = 'Review',
    label_names: tuple[str, ...] = (),
    label_ids: tuple[str, ...] = (),
) -> Issue:
    return Issue(
        id='uuid-r',
        identifier='AI-7',
        title='Add abs',
        description='## Acceptance Criteria\n- abs works\n',
        state_id='state-review',
        state_name=state_name,
        assignee_id='agent-user',
        label_ids=label_ids,
        label_names=label_names,
        parent_id=None,
        branch_name='',
    )


def _spawn_reviewer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result_text: str,
    is_error: bool = False,
) -> None:
    def fake(prompt: str, **_kwargs: object) -> ClaudeRunResult:
        del prompt
        return ClaudeRunResult(
            is_error=is_error,
            exit_code=1 if is_error else 0,
            result_text=result_text,
            total_cost_usd=0.0,
            usage={},
        )

    monkeypatch.setattr(worker_mod, 'spawn_claude', fake)


def test_parse_verdict_finds_marker() -> None:
    assert worker_mod.parse_verdict('summary\n\nVERDICT: APPROVE\n') == 'APPROVE'
    assert (
        worker_mod.parse_verdict('summary\n\nVERDICT: REQUEST_CHANGES\n')
        == 'REQUEST_CHANGES'
    )
    assert worker_mod.parse_verdict('VERDICT: BLOCKED something') is None
    assert worker_mod.parse_verdict('no marker') is None


def test_find_pr_url_in_comments() -> None:
    from albedo.linear_client import Comment

    comments = [
        Comment(id='c1', body='unrelated', author_id='u'),
        Comment(id='c2', body='PR: https://github.com/x/y/pull/3', author_id='u'),
    ]
    assert (
        worker_mod.find_pr_url_in_comments(comments) == 'https://github.com/x/y/pull/3'
    )
    assert worker_mod.find_pr_url_in_comments([]) is None


def test_reviewer_approve_moves_to_awaiting_approval_and_adds_kind_final_pr(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawn_reviewer(monkeypatch, result_text='looks good\n\nVERDICT: APPROVE')
    cfg = _build_cfg(repo, tmp_path)
    fake = _FakeLinear(_review_issue())
    from albedo.linear_client import Comment

    fake.linear_comments = [
        Comment(
            id='c1',
            body='**agent-1**: PR: https://github.com/me/sample/pull/3',
            author_id='u',
        )
    ]
    # Pre-claim it so run_loop's path through run_claimed picks REVIEWER.
    fake._issue = _review_issue()  # type: ignore[attr-defined]

    result = worker_mod.run_claimed(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue=fake._issue,  # type: ignore[attr-defined]
        agent_id='1',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        github_pat=None,
        cli='claude',
    )

    assert result.role.role == 'REVIEWER'
    assert any(c[1].startswith('**agent-1**: REVIEW APPROVE') for c in fake.comments)
    assert len(fake.updates) == 1
    _, update = fake.updates[0]
    assert getattr(update, 'state_id', None) == 'state-await'
    label_ids = getattr(update, 'label_ids', None)
    assert label_ids is not None
    assert 'lab-final' in label_ids


def test_reviewer_request_changes_increments_attempts_and_returns_to_backlog(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawn_reviewer(
        monkeypatch,
        result_text='needs work\n\nVERDICT: REQUEST_CHANGES',
    )
    cfg = _build_cfg(repo, tmp_path)
    fake = _FakeLinear(
        _review_issue(label_names=('attempts:1',), label_ids=('lab-att-1',))
    )

    result = worker_mod.run_claimed(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue=fake._issue,  # type: ignore[attr-defined]
        agent_id='2',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        github_pat=None,
        cli='claude',
    )

    assert result.role.role == 'REVIEWER'
    assert any('REQUEST_CHANGES (attempts=2/3)' in c[1] for c in fake.comments)
    _, update = fake.updates[0]
    assert getattr(update, 'state_id', None) == 'state-backlog'
    assert getattr(update, 'unset_assignee', False) is True
    label_ids = getattr(update, 'label_ids', None)
    assert label_ids is not None
    assert 'lab-att-2' in label_ids
    assert 'lab-att-1' not in label_ids


def test_reviewer_request_changes_escalates_at_max_attempts(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawn_reviewer(
        monkeypatch,
        result_text='still wrong\n\nVERDICT: REQUEST_CHANGES',
    )
    cfg = _build_cfg(repo, tmp_path)
    fake = _FakeLinear(
        _review_issue(label_names=('attempts:2',), label_ids=('lab-att-2',))
    )

    worker_mod.run_claimed(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue=fake._issue,  # type: ignore[attr-defined]
        agent_id='3',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        github_pat=None,
        cli='claude',
    )

    body = fake.comments[0][1]
    assert 'escalating to human' in body
    assert 'attempts=3' in body
    _, update = fake.updates[0]
    assert getattr(update, 'state_id', None) == 'state-await'
    label_ids = getattr(update, 'label_ids', None)
    assert label_ids is not None
    assert 'lab-stuck' in label_ids
    assert 'lab-att-3' in label_ids


def test_reviewer_escalation_threshold_honours_config_override(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawn_reviewer(
        monkeypatch,
        result_text='nope\n\nVERDICT: REQUEST_CHANGES',
    )
    cfg = _build_cfg(repo, tmp_path, max_attempts_before_escalation=1)
    fake = _FakeLinear(_review_issue())

    worker_mod.run_claimed(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue=fake._issue,  # type: ignore[attr-defined]
        agent_id='1',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        github_pat=None,
        cli='claude',
    )

    body = fake.comments[0][1]
    assert 'escalating to human' in body
    assert 'attempts=1' in body
    _, update = fake.updates[0]
    assert getattr(update, 'state_id', None) == 'state-await'
    label_ids = getattr(update, 'label_ids', None)
    assert label_ids is not None
    assert 'lab-stuck' in label_ids


def test_reviewer_no_verdict_treated_as_blocker(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawn_reviewer(monkeypatch, result_text='no marker here')
    cfg = _build_cfg(repo, tmp_path)
    fake = _FakeLinear(_review_issue())

    worker_mod.run_claimed(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue=fake._issue,  # type: ignore[attr-defined]
        agent_id='1',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        github_pat=None,
        cli='claude',
    )

    assert any('BLOCKED' in c[1] for c in fake.comments)
    _, update = fake.updates[0]
    assert getattr(update, 'state_id', None) == 'state-backlog'
    assert getattr(update, 'unset_assignee', False) is True


# --- Phase 5: ARCHITECT decomposition ---------------------------------------


def _good_decomposition_text() -> str:
    return """rationale here

DECOMPOSITION:
{
  "rationale": "split by surface area",
  "children": [
    {
      "title": "Add filter API",
      "description": "Implement /filter endpoint",
      "acceptance_criteria": ["Returns 200 on empty", "Rejects invalid"],
      "estimate": 2
    },
    {
      "title": "Wire UI button",
      "description": "Frontend toggle for filter",
      "acceptance_criteria": ["Button visible", "Triggers API"],
      "estimate": 3
    }
  ]
}
"""


def test_parse_decomposition_extracts_children() -> None:
    decomp = worker_mod.parse_decomposition(_good_decomposition_text())
    assert decomp.rationale == 'split by surface area'
    assert len(decomp.children) == 2
    assert decomp.children[0].title == 'Add filter API'
    assert decomp.children[0].estimate == 2
    assert decomp.children[1].acceptance_criteria == (
        'Button visible',
        'Triggers API',
    )


def test_parse_decomposition_handles_fenced_code() -> None:
    json_only = '\n'.join(_good_decomposition_text().splitlines()[3:])
    fenced = 'preamble\n\nDECOMPOSITION:\n```json\n' + json_only + '\n```\n'
    decomp = worker_mod.parse_decomposition(fenced)
    assert len(decomp.children) == 2


def test_parse_decomposition_rejects_missing_header() -> None:
    with pytest.raises(worker_mod.DecompositionParseError, match='no DECOMPOSITION'):
        worker_mod.parse_decomposition('{"children": []}')


def test_parse_decomposition_rejects_invalid_json() -> None:
    with pytest.raises(worker_mod.DecompositionParseError, match='invalid JSON'):
        worker_mod.parse_decomposition('DECOMPOSITION:\n{not valid')


def test_parse_decomposition_rejects_too_few_children() -> None:
    text = (
        'DECOMPOSITION:\n'
        '{"children": [{"title": "x", "description": "y", '
        '"acceptance_criteria": ["a"], "estimate": 1}]}'
    )
    with pytest.raises(worker_mod.DecompositionParseError, match='length must be'):
        worker_mod.parse_decomposition(text)


def test_parse_decomposition_rejects_bad_estimate() -> None:
    bad = '{"title": "x", "description": "y", '
    bad += '"acceptance_criteria": ["a"], "estimate": 4}'
    ok = '{"title": "z", "description": "y", '
    ok += '"acceptance_criteria": ["b"], "estimate": 2}'
    text = 'DECOMPOSITION:\n{"children": [' + bad + ',' + ok + ']}'
    with pytest.raises(worker_mod.DecompositionParseError, match='estimate'):
        worker_mod.parse_decomposition(text)


def test_parse_decomposition_rejects_empty_ac() -> None:
    bad = '{"title": "x", "description": "y", '
    bad += '"acceptance_criteria": [], "estimate": 1}'
    ok = '{"title": "z", "description": "y", '
    ok += '"acceptance_criteria": ["b"], "estimate": 2}'
    text = 'DECOMPOSITION:\n{"children": [' + bad + ',' + ok + ']}'
    with pytest.raises(worker_mod.DecompositionParseError, match='non-empty list'):
        worker_mod.parse_decomposition(text)


class _ArchitectFakeLinear(_FakeLinear):
    def __init__(self, issue: Issue) -> None:
        super().__init__(issue)
        self.created_issues: list[dict[str, object]] = []
        self.archived: list[str] = []
        self.team_states.update(
            {'Triage': 'state-triage', 'Canceled': 'state-canceled'}
        )
        self.team_labels.update(
            {
                'kind:decomposition': 'lab-kind-decomp',
                'blocked-external': 'lab-blocked-ext',
                'awaiting-human-reply': 'lab-await-human',
            }
        )
        self.existing_children: list[Issue] = []

    def query(self, document: str, _variables: object) -> dict[str, object]:
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
        if 'TeamForIssue' in document:
            return {'issue': {'team': {'id': 'team-1'}}}
        if 'ProjectForIssue' in document:
            return {'issue': {'project': None}}
        return {
            'issue': {
                'team': {
                    'states': {
                        'nodes': [
                            {'id': sid, 'name': name}
                            for name, sid in self.team_states.items()
                        ]
                    }
                }
            }
        }

    def list_children(self, _parent_id: str) -> list[Issue]:
        return list(self.existing_children)

    def archive_issue(self, issue_id: str) -> None:
        self.archived.append(issue_id)

    def create_issue(
        self,
        *,
        team_id: str,
        title: str,
        description: str,
        parent_id: str | None = None,
        label_ids: tuple[str, ...] = (),
        estimate: int | None = None,
        state_id: str | None = None,
        project_id: str | None = None,
    ) -> Issue:
        self.created_issues.append(
            {
                'team_id': team_id,
                'title': title,
                'description': description,
                'parent_id': parent_id,
                'project_id': project_id,
                'label_ids': label_ids,
                'estimate': estimate,
                'state_id': state_id,
            }
        )
        idx = len(self.created_issues)
        return Issue(
            id=f'uuid-c{idx}',
            identifier=f'AI-{100 + idx}',
            title=title,
            description=description,
            state_id=state_id or 'state-backlog',
            state_name='Backlog',
            assignee_id=None,
            label_ids=label_ids,
            label_names=tuple(
                name for name, lid in self.team_labels.items() if lid in label_ids
            ),
            parent_id=parent_id,
            branch_name='',
        )


def _triage_issue() -> Issue:
    return Issue(
        id='uuid-parent',
        identifier='AI-50',
        title='Build feature X',
        description='High-level feature description',
        state_id='state-triage',
        state_name='Triage',
        assignee_id='agent-user',
        label_ids=(),
        label_names=(),
        parent_id=None,
        branch_name='',
    )


def _spawn_architect(
    monkeypatch: pytest.MonkeyPatch, *, result_text: str, is_error: bool = False
) -> None:
    def fake(prompt: str, **_kwargs: object) -> ClaudeRunResult:
        del prompt
        return ClaudeRunResult(
            is_error=is_error,
            exit_code=1 if is_error else 0,
            result_text=result_text,
            total_cost_usd=0.0,
            usage={},
        )

    monkeypatch.setattr(worker_mod, 'spawn_claude', fake)


def test_architect_creates_children_and_moves_parent_to_awaiting_approval(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawn_architect(monkeypatch, result_text=_good_decomposition_text())
    cfg = _build_cfg(repo, tmp_path)
    fake = _ArchitectFakeLinear(_triage_issue())

    worker_mod.run_claimed(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue=fake._issue,  # type: ignore[attr-defined]
        agent_id='1',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        github_pat=None,
        cli='claude',
    )

    assert len(fake.created_issues) == 2
    titles = [c['title'] for c in fake.created_issues]
    assert titles == ['Add filter API', 'Wire UI button']
    estimates = [c['estimate'] for c in fake.created_issues]
    assert estimates == [2, 3]
    for child in fake.created_issues:
        assert child['parent_id'] == 'uuid-parent'
        child_labels = cast('tuple[str, ...]', child['label_ids'])
        assert child_labels == ()
        assert child['state_id'] == 'state-triage'

    body = fake.comments[-1][1]
    assert 'DECOMPOSITION (2 children)' in body
    assert 'AI-101' in body and 'AI-102' in body

    _, update = fake.updates[0]
    assert getattr(update, 'state_id', None) == 'state-await'
    label_ids = getattr(update, 'label_ids', ())
    assert 'lab-kind-decomp' in label_ids
    assert getattr(update, 'unset_assignee', False) is True


def test_architect_archivesexisting_children_on_rerun(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawn_architect(monkeypatch, result_text=_good_decomposition_text())
    cfg = _build_cfg(repo, tmp_path)
    fake = _ArchitectFakeLinear(_triage_issue())
    fake.existing_children = [
        Issue(
            id='uuid-old1',
            identifier='AI-90',
            title='old',
            description='',
            state_id='state-backlog',
            state_name='Backlog',
            assignee_id=None,
            label_ids=(),
            label_names=(),
            parent_id='uuid-parent',
            branch_name='',
        ),
        Issue(
            id='uuid-old2',
            identifier='AI-91',
            title='older',
            description='',
            state_id='state-backlog',
            state_name='Backlog',
            assignee_id=None,
            label_ids=(),
            label_names=(),
            parent_id='uuid-parent',
            branch_name='',
        ),
    ]

    worker_mod.run_claimed(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue=fake._issue,  # type: ignore[attr-defined]
        agent_id='1',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        github_pat=None,
        cli='claude',
    )

    assert fake.archived == ['uuid-old1', 'uuid-old2']
    assert 'Archived 2 previous child(ren): AI-90, AI-91' in fake.comments[-1][1]


def test_architect_handles_blocked_question(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawn_architect(
        monkeypatch, result_text='BLOCKED: which storage backend should we use?'
    )
    cfg = _build_cfg(repo, tmp_path)
    fake = _ArchitectFakeLinear(_triage_issue())

    worker_mod.run_claimed(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue=fake._issue,  # type: ignore[attr-defined]
        agent_id='1',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        github_pat=None,
        cli='claude',
    )

    assert fake.created_issues == []
    body = fake.comments[0][1]
    assert 'BLOCKED: which storage backend' in body
    _, update = fake.updates[0]
    assert getattr(update, 'state_id', None) == 'state-triage'
    label_ids = getattr(update, 'label_ids', ())
    assert 'lab-blocked-ext' not in label_ids
    # Question-class blocker: gate the issue from re-pickup until a human
    # comment arrives (otherwise workers loop on it).
    assert 'lab-await-human' in label_ids


def test_architect_handles_question_marker(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawn_architect(
        monkeypatch, result_text='QUESTION: which storage backend should we use?'
    )
    cfg = _build_cfg(repo, tmp_path)
    fake = _ArchitectFakeLinear(_triage_issue())

    worker_mod.run_claimed(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue=fake._issue,  # type: ignore[attr-defined]
        agent_id='1',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        github_pat=None,
        cli='claude',
    )

    assert fake.created_issues == []
    body = fake.comments[0][1]
    # Marker stripped from human-facing comment; agent prefix kept.
    assert body.startswith('**agent-1**: which storage backend')
    assert 'QUESTION:' not in body
    _, update = fake.updates[0]
    assert getattr(update, 'state_id', None) == 'state-triage'
    label_ids = getattr(update, 'label_ids', ())
    assert 'lab-blocked-ext' not in label_ids
    assert 'lab-await-human' in label_ids


def test_architect_question_captures_multiline_markdown(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = (
        'QUESTION: What scope did you have in mind?\n'
        '\n'
        '- Option A: just stdlib `logging`\n'
        '- Option B: structured JSON logs\n'
        '\n'
        'Please pick one.'
    )
    _spawn_architect(monkeypatch, result_text=payload)
    cfg = _build_cfg(repo, tmp_path)
    fake = _ArchitectFakeLinear(_triage_issue())

    worker_mod.run_claimed(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue=fake._issue,  # type: ignore[attr-defined]
        agent_id='2',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        github_pat=None,
        cli='claude',
    )

    body = fake.comments[0][1]
    assert body.startswith('**agent-2**: What scope did you have in mind?')
    assert '- Option A: just stdlib `logging`' in body
    assert '- Option B: structured JSON logs' in body
    assert 'Please pick one.' in body
    assert 'QUESTION:' not in body


def test_architect_blocked_epic_split_adds_blocked_external(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawn_architect(monkeypatch, result_text='BLOCKED: needs epic split')
    cfg = _build_cfg(repo, tmp_path)
    fake = _ArchitectFakeLinear(_triage_issue())

    worker_mod.run_claimed(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue=fake._issue,  # type: ignore[attr-defined]
        agent_id='1',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        github_pat=None,
        cli='claude',
    )

    _, update = fake.updates[0]
    assert getattr(update, 'state_id', None) == 'state-triage'
    label_ids = getattr(update, 'label_ids', ())
    assert 'lab-blocked-ext' in label_ids


def test_architect_unparseable_output_treated_as_blocker(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawn_architect(monkeypatch, result_text='no markers here at all')
    cfg = _build_cfg(repo, tmp_path)
    fake = _ArchitectFakeLinear(_triage_issue())

    worker_mod.run_claimed(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue=fake._issue,  # type: ignore[attr-defined]
        agent_id='1',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        github_pat=None,
        cli='claude',
    )

    assert fake.created_issues == []
    assert fake.archived == []
    assert 'BLOCKED' in fake.comments[0][1]
    _, update = fake.updates[0]
    assert getattr(update, 'state_id', None) == 'state-triage'


def test_cancel_pattern_finds_reason() -> None:
    match = worker_mod.CANCEL_PATTERN.search(
        'preamble\n\nCANCEL: reporter retracted the issue\n'
    )
    assert match is not None
    assert match.group(1) == 'reporter retracted the issue'
    assert worker_mod.CANCEL_PATTERN.search('no marker') is None
    # Must anchor at line start: "DO NOT CANCEL: x" should not match.
    assert worker_mod.CANCEL_PATTERN.search('do not CANCEL: bypass guard') is None


def _triage_issue_with_awaiting_label() -> Issue:
    return Issue(
        id='uuid-parent',
        identifier='AI-50',
        title='Build feature X',
        description='High-level feature description',
        state_id='state-triage',
        state_name='Triage',
        assignee_id='agent-user',
        label_ids=('lab-await-human',),
        label_names=('awaiting-human-reply',),
        parent_id=None,
        branch_name='',
    )


def test_architect_cancel_transitions_to_canceled(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawn_architect(
        monkeypatch,
        result_text='user retracted\n\nCANCEL: reporter said "i was wrong"',
    )
    cfg = _build_cfg(repo, tmp_path)
    fake = _ArchitectFakeLinear(_triage_issue_with_awaiting_label())

    worker_mod.run_claimed(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue=fake._issue,  # type: ignore[attr-defined]
        agent_id='3',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        github_pat=None,
        cli='claude',
    )

    assert fake.created_issues == []
    assert fake.archived == []
    body = fake.comments[0][1]
    assert body.startswith('**agent-3**: CANCELLED:')
    assert 'i was wrong' in body
    _, update = fake.updates[0]
    assert getattr(update, 'state_id', None) == 'state-canceled'
    assert getattr(update, 'unset_assignee', False) is True
    label_ids = getattr(update, 'label_ids', ())
    assert 'lab-await-human' not in label_ids


def test_architect_cancel_takes_priority_over_blocked(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawn_architect(
        monkeypatch,
        result_text=(
            'CANCEL: reporter retracted\nBLOCKED: should I have asked instead?\n'
        ),
    )
    cfg = _build_cfg(repo, tmp_path)
    fake = _ArchitectFakeLinear(_triage_issue())

    worker_mod.run_claimed(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue=fake._issue,  # type: ignore[attr-defined]
        agent_id='1',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        github_pat=None,
        cli='claude',
    )

    body = fake.comments[0][1]
    assert 'CANCELLED' in body
    assert 'BLOCKED' not in body
    _, update = fake.updates[0]
    assert getattr(update, 'state_id', None) == 'state-canceled'
    label_ids = getattr(update, 'label_ids', ())
    assert 'lab-await-human' not in label_ids


def test_architect_cancel_falls_back_when_canceled_state_missing(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawn_architect(monkeypatch, result_text='CANCEL: reporter retracted')
    cfg = _build_cfg(repo, tmp_path)
    fake = _ArchitectFakeLinear(_triage_issue())
    del fake.team_states['Canceled']

    worker_mod.run_claimed(
        config=cfg,
        linear=fake,  # type: ignore[arg-type]
        issue=fake._issue,  # type: ignore[attr-defined]
        agent_id='1',
        prompts_dir=bundled_prompts_dir(),
        mcp_config_path=None,
        github_pat=None,
        cli='claude',
    )

    body = fake.comments[0][1]
    assert 'CANCELLED' in body
    _, update = fake.updates[0]
    # Architect's target_state_on_blocker is Triage.
    assert getattr(update, 'state_id', None) == 'state-triage'
