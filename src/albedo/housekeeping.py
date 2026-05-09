"""Periodic housekeeping outside the per-task pipeline.

Currently ships:
- startup-time stale-claim recovery (Phase 3),
- decomposition approval watcher (Phase 5, Variant A): when a parent issue
  with the `kind:decomposition` label transitions to Todo, move all of its
  children from Triage to Backlog so workers can pick them up,
- merged-PR watcher (Phase 6): when a Linear issue's PR has been merged on
  GitHub, automatically move the issue to Done.

Phase 6 also adds worktree GC.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from albedo.claim_manifest import (
    claim_manifest_path,
    clear_claim_manifest,
    read_claim_manifest,
)
from albedo.github_client import GithubClient, GithubError
from albedo.github_client import parse_pr_url as parse_github_pr_url
from albedo.heartbeat import (
    DEFAULT_STALE_AFTER_SECONDS,
    heartbeat_path,
    is_stale,
)
from albedo.linear_client import (
    ISSUE_FRAGMENT,
    Issue,
    IssueUpdate,
    LinearClient,
    issue_from_node,
)
from albedo.worker import (
    CANCELLED_BY_HUMAN_LABEL,
    find_pr_url_in_comments,
    log_state_transition,
    release_claim_assignee,
)
from albedo.worktree import (
    WorktreeInfo,
    has_uncommitted_changes,
    has_unpushed_commits,
    list_worktrees,
    remove_worktree,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StaleClaimReport:
    inspected: int
    released: list[Issue]


def recover_stale_claims(
    *,
    linear: LinearClient,
    state_dir: Path,
    agent_user_id: str,
    active_agent_ids: list[str] | None = None,
    max_heartbeat_age_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    project_id: str | None = None,
) -> StaleClaimReport:
    """Un-assign issues whose worker heartbeat is missing or stale.

    `active_agent_ids` optionally lists workers we're about to launch in this
    process — their heartbeats are about to be touched, so we treat them as
    "fresh" even if currently missing on disk. This avoids the boot-time
    race where the very first iteration sees stale heartbeats for workers
    that are starting right now.

    `project_id` scopes recovery to a single Linear project so a profile
    switch (e.g. yesterday `--project sample`, today `--project myrepo`)
    doesn't accidentally release the other profile's stale claims.
    """
    assigned = linear.list_assigned_issues(agent_user_id, project_id=project_id)
    fresh: set[str] = set(active_agent_ids or [])
    released: list[Issue] = []
    for issue in assigned:
        agent_id = _agent_id_from_branch(issue.branch_name) or _agent_id_from_labels(
            issue.label_names
        )
        if agent_id in fresh:
            continue
        if agent_id is None:
            # No marker tying issue to a specific worker — heuristic: if any
            # known agent file is fresh, leave it (someone might own it).
            if _any_fresh(state_dir, fresh, max_heartbeat_age_seconds):
                continue
        elif not is_stale(
            heartbeat_path(state_dir, agent_id),
            max_age_seconds=max_heartbeat_age_seconds,
        ):
            continue
        _release_with_state_restore(
            linear=linear,
            issue=issue,
            agent_id=agent_id,
            state_dir=state_dir,
        )
        released.append(issue)
    return StaleClaimReport(inspected=len(assigned), released=released)


def _release_with_state_restore(
    *,
    linear: LinearClient,
    issue: Issue,
    agent_id: str | None,
    state_dir: Path,
) -> None:
    """Un-assign and restore state if a claim manifest exists for this agent.

    If the worker had moved the issue to `In Progress` and crashed, the
    manifest at `state_dir/agent-<id>.claim.json` records the prior
    `state_id`. Recover by rolling state back in the same Linear update
    that clears the assignee. Without an agent_id (no marker) or
    manifest, fall through to a plain assignee release — recovery still
    matches today's behaviour.
    """
    if agent_id is not None:
        manifest_path = claim_manifest_path(state_dir, agent_id)
        manifest = read_claim_manifest(manifest_path)
        if manifest is not None and manifest.issue_id == issue.id:
            try:
                linear.update_issue(
                    issue.id,
                    IssueUpdate(state_id=manifest.prev_state_id, unset_assignee=True),
                )
                clear_claim_manifest(manifest_path)
                return
            except Exception as exc:
                log.warning(
                    'state-restore for stale claim %s failed (%s); '
                    'falling back to assignee-only release',
                    issue.identifier,
                    exc,
                )
    release_claim_assignee(linear=linear, issue=issue)


_AGENT_BRANCH = re.compile(r'/agent-(?P<id>[^/]+)/')
_AGENT_LABEL = re.compile(r'^agent-(?P<id>.+)$')


def _agent_id_from_branch(branch_name: str) -> str | None:
    match = _AGENT_BRANCH.search(branch_name or '')
    return match.group('id') if match else None


def _agent_id_from_labels(label_names: tuple[str, ...]) -> str | None:
    for name in label_names:
        match = _AGENT_LABEL.match(name)
        if match:
            return match.group('id')
    return None


def _any_fresh(state_dir: Path, _exclude: set[str], max_age_seconds: int) -> bool:
    if not state_dir.exists():
        return False
    for entry in state_dir.glob('agent-*.heartbeat'):
        if not is_stale(entry, max_age_seconds=max_age_seconds):
            return True
    return False


@dataclass(frozen=True, slots=True)
class DecompositionReleaseReport:
    parents_processed: int
    children_released: list[Issue]
    children_cancelled: list[Issue] = field(default_factory=list[Issue])


def release_decomposed_children(
    *,
    linear: LinearClient,
    team_id: str,
    project_id: str | None = None,
) -> DecompositionReleaseReport:
    """Move children of approved decomposition parents from Triage to Backlog.

    Variant A flow: ARCHITECT creates children in `Triage` state with a
    non-null parent. Workers skip such issues (Triage + parent_id is not
    None). On parent approval (parent moves to Todo with the
    `kind:decomposition` label), this watcher moves each child to
    `Backlog`, where the regular Coder pickup applies. The parent stays
    visibly in-progress (state type `unstarted`) while children execute
    rather than being marked Done before any work begins; worker pickup
    states (`Triage`, `Backlog`, `Review`) exclude `Todo`, so the parent
    is not re-picked.

    Triage children carrying the `cancelled-by-human` label are dropped
    instead of released — moved to `Canceled` so they don't linger in
    Triage. This is the human-edit affordance: between Architect output
    and parent approval, the human can prune the plan by labelling a
    child cancelled.

    Idempotent: children already past Triage are skipped, so re-running
    the watcher is a no-op.
    """
    parents = _list_decomposition_parents_approved(linear, team_id, project_id)
    released: list[Issue] = []
    cancelled: list[Issue] = []
    backlog_state_id = _find_state_id(linear, team_id, 'Backlog')
    if backlog_state_id is None:
        log.warning(
            'housekeeping: no Backlog state found in team %s; skipping release',
            team_id,
        )
        return DecompositionReleaseReport(
            parents_processed=len(parents),
            children_released=[],
            children_cancelled=[],
        )
    cancelled_state_id = _find_state_id(linear, team_id, 'Canceled')

    for parent in parents:
        children = linear.list_children(parent.id)
        any_released_for_parent: list[Issue] = []
        any_cancelled_for_parent: list[Issue] = []
        for child in children:
            if child.state_name != 'Triage':
                continue
            if CANCELLED_BY_HUMAN_LABEL in child.label_names:
                if cancelled_state_id is None:
                    log.warning(
                        'housekeeping: %s carries cancelled-by-human but no '
                        'Canceled state exists in team %s; leaving it in Triage',
                        child.identifier,
                        team_id,
                    )
                    continue
                linear.update_issue(child.id, IssueUpdate(state_id=cancelled_state_id))
                log_state_transition(
                    child.identifier,
                    'Triage',
                    'Canceled',
                    reason='cancelled-by-human label',
                )
                cancelled.append(child)
                any_cancelled_for_parent.append(child)
                continue
            linear.update_issue(child.id, IssueUpdate(state_id=backlog_state_id))
            log_state_transition(
                child.identifier,
                'Triage',
                'Backlog',
                reason=f'decomposition approved (parent {parent.identifier})',
            )
            released.append(child)
            any_released_for_parent.append(child)
        if any_released_for_parent or any_cancelled_for_parent:
            parts: list[str] = []
            if any_released_for_parent:
                parts.append(
                    'released '
                    + ', '.join(c.identifier for c in any_released_for_parent)
                    + ' to Backlog for pickup'
                )
            if any_cancelled_for_parent:
                parts.append(
                    'cancelled '
                    + ', '.join(c.identifier for c in any_cancelled_for_parent)
                    + ' (cancelled-by-human label)'
                )
            linear.add_comment(
                parent.id,
                '**housekeeping**: Decomposition approved — ' + '; '.join(parts) + '.',
            )
    return DecompositionReleaseReport(
        parents_processed=len(parents),
        children_released=released,
        children_cancelled=cancelled,
    )


@dataclass(frozen=True, slots=True)
class DecompositionRollupReport:
    parents_completed: list[Issue]


def complete_decompositions_when_children_done(
    *,
    linear: LinearClient,
    team_id: str,
    project_id: str | None = None,
) -> DecompositionRollupReport:
    """Move a decomposition parent to Done once all of its children are finished.

    After approval (AI-100), the parent sits in Todo while children execute.
    This watcher rolls the parent up to Done when every child has reached a
    terminal state (Done / Canceled / Cancelled / Duplicate). Without this,
    parents accumulate in Todo until a human closes them manually.

    Defensive: a parent with zero children is left untouched. Idempotent: a
    parent already in a completed state is not in the candidate set, so
    rerunning the rollup is a no-op.
    """
    parents = _list_decomposition_parents_approved(linear, team_id, project_id)
    done_state_id = _find_state_id(linear, team_id, 'Done')
    if done_state_id is None:
        log.warning(
            'housekeeping: no Done state found in team %s; skipping rollup',
            team_id,
        )
        return DecompositionRollupReport(parents_completed=[])

    completed: list[Issue] = []
    for parent in parents:
        children = linear.list_children(parent.id)
        if not children:
            continue
        if not all(child.state_name in _FINISHED_STATE_NAMES for child in children):
            continue
        linear.update_issue(parent.id, IssueUpdate(state_id=done_state_id))
        linear.add_comment(
            parent.id,
            '**housekeeping**: All decomposition children finished — closing parent.',
        )
        completed.append(parent)
        log_state_transition(
            parent.identifier,
            'Todo',
            'Done',
            reason=f'decomposition rollup, {len(children)} children',
        )
    return DecompositionRollupReport(parents_completed=completed)


def _list_decomposition_parents_approved(
    linear: LinearClient,
    team_id: str,
    project_id: str | None = None,
) -> list[Issue]:
    document = (
        'query DecompositionParents($filter: IssueFilter!) { '
        'issues(filter: $filter, first: 50) { nodes {' + ISSUE_FRAGMENT + '} } }'
    )
    filt: dict[str, Any] = {
        'team': {'id': {'eq': team_id}},
        'labels': {'name': {'eq': 'kind:decomposition'}},
        'state': {'name': {'eq': 'Todo'}, 'type': {'eq': 'unstarted'}},
    }
    if project_id is not None:
        filt['project'] = {'id': {'eq': project_id}}
    data = linear.query(document, {'filter': filt})
    return [issue_from_node(node) for node in data['issues']['nodes']]


def _find_state_id(linear: LinearClient, team_id: str, name: str) -> str | None:
    """Look up a single workflow state id by name on a team."""
    document = """
        query StateByName($teamId: ID!) {
          workflowStates(filter: {team: {id: {eq: $teamId}}}, first: 50) {
            nodes { id name }
          }
        }
    """
    data = linear.query(document, {'teamId': team_id})
    for n in data['workflowStates']['nodes']:
        if n['name'] == name:
            return str(n['id'])
    return None


# --- Phase 6: PR merge → Done watcher ----------------------------------------


@dataclass(frozen=True, slots=True)
class MergedPrReleaseReport:
    inspected: int
    moved_to_done: list[Issue]


def sync_merged_prs_to_done(
    *,
    linear: LinearClient,
    github: GithubClient,
    team_id: str,
    project_id: str | None = None,
) -> MergedPrReleaseReport:
    """Move Linear issues to Done when their PR has been merged on GitHub.

    Scope: Linear issues in `Awaiting approval` with `kind:final-pr` label
    (this is the state Reviewer leaves them in after APPROVE). For each, we
    parse the `PR: <url>` marker out of the issue's comments, ask GitHub
    whether that PR is merged, and if so move the issue to Done.

    Idempotent: an issue already in Done is not in the candidate set;
    failures on individual issues are logged and other candidates still
    proceed.
    """
    candidates = _list_awaiting_approval_with_final_pr(linear, team_id, project_id)
    done_state_id = _find_state_id(linear, team_id, 'Done')
    if done_state_id is None:
        log.warning(
            'housekeeping: no Done state in team %s; skipping merge sync',
            team_id,
        )
        return MergedPrReleaseReport(inspected=len(candidates), moved_to_done=[])

    moved: list[Issue] = []
    for issue in candidates:
        try:
            comments = linear.list_comments(issue.id)
        except Exception as exc:
            log.warning(
                'housekeeping: list_comments(%s) failed: %s',
                issue.identifier,
                exc,
            )
            continue
        url = find_pr_url_in_comments(comments)
        if url is None:
            continue
        coords = parse_github_pr_url(url)
        if coords is None:
            continue
        owner, repo, number = coords
        try:
            pr = github.get_pull_request(owner, repo, number)
        except GithubError as exc:
            log.warning(
                'housekeeping: get_pull_request(%s/%s#%d) failed: %s',
                owner,
                repo,
                number,
                exc,
            )
            continue
        if not pr.merged:
            continue
        try:
            linear.update_issue(issue.id, IssueUpdate(state_id=done_state_id))
        except Exception as exc:
            log.warning(
                'housekeeping: failed to move %s to Done: %s',
                issue.identifier,
                exc,
            )
            continue
        moved.append(issue)
        log_state_transition(
            issue.identifier,
            'Awaiting approval',
            'Done',
            reason=f'PR {owner}/{repo}#{number} merged',
        )

    return MergedPrReleaseReport(inspected=len(candidates), moved_to_done=moved)


def _list_awaiting_approval_with_final_pr(
    linear: LinearClient, team_id: str, project_id: str | None = None
) -> list[Issue]:
    document = (
        'query AwaitingFinalPr($filter: IssueFilter!) { '
        'issues(filter: $filter, first: 50) { nodes {' + ISSUE_FRAGMENT + '} } }'
    )
    filt: dict[str, Any] = {
        'team': {'id': {'eq': team_id}},
        'state': {'name': {'eq': 'Awaiting approval'}},
        'labels': {'name': {'eq': 'kind:final-pr'}},
    }
    if project_id is not None:
        filt['project'] = {'id': {'eq': project_id}}
    data = linear.query(document, {'filter': filt})
    return [issue_from_node(node) for node in data['issues']['nodes']]


# --- Phase 6: worktree GC ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorktreeGcReport:
    inspected: int
    removed: list[str]


_TASK_BRANCH_RE = re.compile(r'^task/(?P<id>[A-Za-z0-9-]+)$')


def gc_worktrees(
    *,
    linear: LinearClient,
    repo_path: Path,
) -> WorktreeGcReport:
    """Remove worktrees whose Linear issue is finished and that are clean.

    For each `task/<id>` worktree, we look up the Linear issue: if its
    state type is `completed` or `canceled`, AND the worktree has no
    uncommitted changes AND no unpushed commits, the worktree is removed
    (the branch is preserved on the remote, only the local checkout
    disappears).

    Anything ambiguous is left alone — never delete code we're not sure
    about.
    """
    try:
        worktrees = list_worktrees(repo_path)
    except Exception as exc:
        log.warning('worktree GC: list_worktrees failed: %s', exc)
        return WorktreeGcReport(inspected=0, removed=[])

    candidates: list[tuple[WorktreeInfo, str]] = []
    for wt in worktrees:
        match = _TASK_BRANCH_RE.match(wt.branch)
        if match is None:
            continue
        candidates.append((wt, match.group('id').upper()))

    if not candidates:
        return WorktreeGcReport(inspected=len(worktrees), removed=[])

    identifiers = [identifier for _, identifier in candidates]
    try:
        issues = linear.list_issues_by_identifiers(identifiers)
    except Exception as exc:
        log.warning('worktree GC: batched issue lookup failed: %s', exc)
        return WorktreeGcReport(inspected=len(worktrees), removed=[])
    by_identifier = {issue.identifier: issue for issue in issues}

    removed: list[str] = []
    for wt, identifier in candidates:
        issue = by_identifier.get(identifier)
        if issue is None:
            log.warning('worktree GC: issue %s not found in Linear', identifier)
            continue
        if not _state_is_finished(issue.state_name):
            continue
        if has_uncommitted_changes(wt.path):
            log.info('worktree GC: keeping %s (uncommitted changes)', wt.path)
            continue
        if has_unpushed_commits(wt.path):
            log.info('worktree GC: keeping %s (unpushed commits)', wt.path)
            continue
        try:
            remove_worktree(repo_path, wt.path)
        except Exception as exc:
            log.warning('worktree GC: remove(%s) failed: %s', wt.path, exc)
            continue
        removed.append(str(wt.path))
        log.info('worktree GC: removed %s (issue %s done)', wt.path, identifier)

    return WorktreeGcReport(inspected=len(worktrees), removed=removed)


_FINISHED_STATE_NAMES = frozenset({'Done', 'Canceled', 'Cancelled', 'Duplicate'})


def _state_is_finished(state_name: str) -> bool:
    return state_name in _FINISHED_STATE_NAMES


# --- Phase 7: archive Done issues older than N days -----------------------


@dataclass(frozen=True, slots=True)
class ArchiveReport:
    inspected: int
    archived: list[str]


def archive_old_done_issues(
    *,
    linear: LinearClient,
    team_id: str,
    older_than_days: int,
    now: datetime | None = None,
    page_size: int = 100,
    project_id: str | None = None,
) -> ArchiveReport:
    """Archive issues completed more than `older_than_days` ago.

    Linear treats archived issues as hidden but recoverable — the data
    isn't deleted. Done columns accumulate forever otherwise, slowing
    down every poll over the team's history.

    `older_than_days <= 0` disables the job (returns an empty report).
    Skips issues that have no `completedAt` (defensive: in theory all
    completed-state issues have it, but we never want to archive
    something just because Linear is mid-transition).
    """
    if older_than_days <= 0:
        return ArchiveReport(inspected=0, archived=[])

    cutoff = (now or datetime.now(UTC)) - timedelta(days=older_than_days)
    cutoff_iso = cutoff.isoformat()
    document = (
        'query OldDone($filter: IssueFilter!, $first: Int!) { '
        'issues(filter: $filter, first: $first) { nodes {'
        + ISSUE_FRAGMENT
        + ', completedAt} } }'
    )
    filt: dict[str, Any] = {
        'team': {'id': {'eq': team_id}},
        'state': {'type': {'eq': 'completed'}},
        'completedAt': {'lt': cutoff_iso},
    }
    if project_id is not None:
        filt['project'] = {'id': {'eq': project_id}}
    try:
        data = linear.query(document, {'filter': filt, 'first': page_size})
    except Exception as exc:
        log.warning('archive: list-old-done failed: %s', exc)
        return ArchiveReport(inspected=0, archived=[])

    nodes = data.get('issues', {}).get('nodes', [])
    archived: list[str] = []
    for node in nodes:
        if not node.get('completedAt'):
            continue
        issue = issue_from_node(node)
        try:
            linear.archive_issue(issue.id)
        except Exception as exc:
            log.warning('archive: %s failed: %s', issue.identifier, exc)
            continue
        archived.append(issue.identifier)
        log.info(
            'archive: %s (completed %s, > %d days)',
            issue.identifier,
            node['completedAt'],
            older_than_days,
        )
    return ArchiveReport(inspected=len(nodes), archived=archived)
