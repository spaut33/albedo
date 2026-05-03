"""Pure utilities for the CI-failure redispatch flow.

Mirrors `comment_redispatch.py` for CI runs: extract the Linear
identifier from a branch name, truncate the failing-step log tail,
format the comment body posted back on the issue, and round-trip a
per-issue dedup state file mapping `issue_id -> last seen completed
run id`.

No Linear or GitHub orchestration lives here — those land in the next
child of AI-56. This module is import-pure: only `re`, `json`,
`logging`, and `pathlib`.

Loop avoidance: per-issue `last_ci_run.json` records the most recently
observed completed run id. A new run id fires once, then the file pins
it so the next tick skips. The file is updated on first observation
without firing — runs that pre-date the feature roll-out don't trigger
spurious comments.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from albedo.comment_redispatch import (
    _strip_pickup_blockers,  # pyright: ignore[reportPrivateUsage]
    _team_labels_for_issue,  # pyright: ignore[reportPrivateUsage]
)
from albedo.github_client import (
    GithubClient,
    GithubError,
    PullRequest,
    WorkflowRun,
)
from albedo.linear_client import (
    ISSUE_FRAGMENT,
    Issue,
    IssueUpdate,
    LinearClient,
    issue_from_node,
)

log = logging.getLogger(__name__)

LAST_CI_RUN_STATE_FILE = 'last_ci_run.json'
LAST_CI_RUN_STATE_VERSION = 1

DEFAULT_LOG_TAIL_LINES = 200

# Case-insensitive Linear identifier (e.g. AI-58, eng-12). `\b` anchors
# keep us off `notai-58`-style false positives. First match wins, so a
# branch like `task/AI-58-fix-AI-12` resolves to AI-58.
_IDENTIFIER_RE = re.compile(r'\b([a-z]+-\d+)\b', re.IGNORECASE)


def extract_linear_identifier(branch: str) -> str | None:
    """Return the first Linear identifier in `branch`, upper-cased, else None.

    Independent of any `task/` prefix — bare branches like `AI-58` and
    nested branches like `feature/AI-58-foo` both resolve.
    """
    match = _IDENTIFIER_RE.search(branch)
    if match is None:
        return None
    return match.group(1).upper()


def truncate_log_tail(log: str, max_lines: int = DEFAULT_LOG_TAIL_LINES) -> str:
    """Return at most the last `max_lines` lines of `log`.

    When the input has more than `max_lines` lines, prepend a single
    elision marker line: `… (N earlier lines elided)` where N is the
    number of dropped lines. Otherwise the input is returned unchanged.
    """
    if not log:
        return log
    lines = log.splitlines()
    if len(lines) <= max_lines:
        return log
    dropped = len(lines) - max_lines
    kept = lines[-max_lines:]
    marker = f'… ({dropped} earlier lines elided)'
    return '\n'.join([marker, *kept])


def format_ci_failure_comment(
    *,
    workflow_name: str,
    run_url: str,
    job_name: str,
    step_name: str,
    log_tail: str,
) -> str:
    """Render the CI-failure comment body posted back to Linear.

    Shape: header line, `Failed job:` line, fenced ```log block. The
    log block is wrapped verbatim — callers should pass an already
    truncated tail via `truncate_log_tail`.
    """
    header = f'**CI failure**: workflow `{workflow_name}` — [run]({run_url})'
    failed = f'Failed job: `{job_name}` / step `{step_name}`'
    fence = f'```log\n{log_tail}\n```'
    return f'{header}\n{failed}\n{fence}'


def load_last_ci_run(state_dir: Path) -> dict[str, str]:
    """Read the `{issue_id: run_id}` map, tolerating absence and corruption.

    Missing file → empty dict. Corrupt JSON, wrong shape, or wrong
    version → empty dict + logged warning. Mirrors
    `comment_redispatch._load_seen` so both feeds fail soft on a single
    poll cycle without raising into the supervisor.
    """
    path = _state_path(state_dir)
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning('ci_redispatch state read failed at %s: %s', path, exc)
        return {}
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning('ci_redispatch state at %s is corrupt: %s', path, exc)
        return {}
    if not isinstance(parsed, dict):
        log.warning('ci_redispatch state at %s has unexpected shape', path)
        return {}
    data = cast(dict[str, Any], parsed)
    if data.get('version') != LAST_CI_RUN_STATE_VERSION:
        return {}
    runs_raw = data.get('runs', {})
    if not isinstance(runs_raw, dict):
        return {}
    runs_typed = cast(dict[str, Any], runs_raw)
    return {str(k): str(v) for k, v in runs_typed.items()}


def save_last_ci_run(state_dir: Path, mapping: dict[str, str]) -> None:
    """Atomically persist the `{issue_id: run_id}` map.

    Writes through a sibling `.tmp` file then renames so a crashed write
    cannot leave a half-truncated state file behind. Repeated saves of
    the same mapping produce identical bytes (idempotent).
    """
    path = _state_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'version': LAST_CI_RUN_STATE_VERSION,
        'runs': dict(sorted(mapping.items())),
    }
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload), encoding='utf-8')
    tmp.replace(path)


def seed_first_observation(state: dict[str, str], issue_id: str, run_id: str) -> bool:
    """Record `run_id` for `issue_id` only on first observation.

    Returns True when this call seeded a fresh entry (caller should NOT
    fire a comment), False when `issue_id` already had a prior run id
    (caller decides whether the new id warrants firing). Mutates `state`
    in place when seeding. Mirrors `comment_redispatch`'s seed-without-
    fire so historical runs don't replay after rollout.
    """
    if issue_id in state:
        return False
    state[issue_id] = run_id
    return True


def _state_path(state_dir: Path) -> Path:
    return state_dir / LAST_CI_RUN_STATE_FILE


@dataclass(frozen=True, slots=True)
class CiRedispatchResult:
    issue_identifier: str
    run_id: int
    workflow_name: str


@dataclass(frozen=True, slots=True)
class CiRedispatchReport:
    inspected: int = 0
    fired: tuple[CiRedispatchResult, ...] = field(default_factory=tuple)
    seeded: tuple[str, ...] = field(default_factory=tuple)


_AWAITING_APPROVAL_STATE = 'Awaiting approval'
_IN_PROGRESS_STATE = 'In Progress'


def redispatch_on_failed_ci(
    *,
    linear: LinearClient,
    github: GithubClient,
    state_dir: Path,
    repo_owner: str,
    repo_name: str,
    team_id: str,
    project_id: str | None = None,
) -> CiRedispatchReport:
    """Re-route Awaiting-approval issues whose CI has freshly failed.

    For each Awaiting-approval issue, find the open PR whose head branch
    maps to that issue's identifier (cached for one tick), inspect the
    most recent completed workflow run for the PR's branch, and on a
    fresh `conclusion=='failure'` post a Linear comment with the failing
    step's truncated log tail, transition the issue to `In Progress`,
    and strip the human-gating labels.

    Idempotent across ticks via `last_ci_run.json`. On first observation
    of a completed run id for an issue, the run is recorded but no
    comment is fired — historical failures don't replay after rollout.
    """
    seen = load_last_ci_run(state_dir)
    candidates = _list_awaiting_approval_unassigned(linear, team_id, project_id)
    if not candidates:
        return CiRedispatchReport()

    in_progress_state_id = _state_id_by_name(linear, team_id, _IN_PROGRESS_STATE)
    if in_progress_state_id is None:
        log.warning(
            'ci_redispatch: no %r state in team %s; skipping tick',
            _IN_PROGRESS_STATE,
            team_id,
        )
        return CiRedispatchReport(inspected=len(candidates))

    pr_cache = _build_pr_cache(github, repo_owner, repo_name)

    fired: list[CiRedispatchResult] = []
    seeded: list[str] = []

    for issue in candidates:
        pr = pr_cache.get(issue.identifier)
        if pr is None:
            log.debug(
                'ci_redispatch: %s — no open PR matched; skipping',
                issue.identifier,
            )
            continue
        latest = _latest_completed_run(github, repo_owner, repo_name, pr.head_branch)
        if latest is None:
            continue
        run_id_str = str(latest.id)
        prior = seen.get(issue.id)
        if prior == run_id_str:
            continue
        if prior is None:
            seen[issue.id] = run_id_str
            seeded.append(issue.identifier)
            continue
        if latest.conclusion != 'failure':
            seen[issue.id] = run_id_str
            continue
        if not _fire_for_failed_run(
            linear=linear,
            github=github,
            issue=issue,
            run=latest,
            repo_owner=repo_owner,
            repo_name=repo_name,
            in_progress_state_id=in_progress_state_id,
        ):
            continue
        seen[issue.id] = run_id_str
        fired.append(
            CiRedispatchResult(
                issue_identifier=issue.identifier,
                run_id=latest.id,
                workflow_name=latest.name,
            )
        )

    save_last_ci_run(state_dir, seen)
    log.info(
        'ci_redispatch tick: inspected=%d fired=%d seeded=%d',
        len(candidates),
        len(fired),
        len(seeded),
    )
    return CiRedispatchReport(
        inspected=len(candidates),
        fired=tuple(fired),
        seeded=tuple(seeded),
    )


def _list_awaiting_approval_unassigned(
    linear: LinearClient, team_id: str, project_id: str | None
) -> list[Issue]:
    """Return unassigned Awaiting-approval issues for the team/project.

    Uses the same shape as comment_redispatch's human-gated query so the
    two feeds stay symmetric. Filtering on `assignee=null` happens in
    Python — Linear's null filter has been observed to silently drop
    valid candidates in the sister flow.
    """
    document = (
        'query CiAwaitingApproval($filter: IssueFilter!) { '
        'issues(filter: $filter, first: 100) { nodes {' + ISSUE_FRAGMENT + '} } }'
    )
    filt: dict[str, Any] = {
        'team': {'id': {'eq': team_id}},
        'state': {'name': {'eq': _AWAITING_APPROVAL_STATE}},
    }
    if project_id is not None:
        filt['project'] = {'id': {'eq': project_id}}
    data = linear.query(document, {'filter': filt})
    nodes = data['issues']['nodes']
    return [issue_from_node(node) for node in nodes if node.get('assignee') is None]


def _state_id_by_name(linear: LinearClient, team_id: str, name: str) -> str | None:
    document = """
        query StateByName($teamId: ID!) {
          workflowStates(filter: {team: {id: {eq: $teamId}}}, first: 50) {
            nodes { id name }
          }
        }
    """
    data = linear.query(document, {'teamId': team_id})
    for node in data['workflowStates']['nodes']:
        if node['name'] == name:
            return str(node['id'])
    return None


def _build_pr_cache(
    github: GithubClient, owner: str, repo: str
) -> dict[str, PullRequest]:
    """List open PRs once and key them by extracted Linear identifier.

    First match wins when two open PRs map to the same identifier — the
    list endpoint orders newest-first by default, so the most recent PR
    on a given identifier takes precedence. Branches that don't carry a
    Linear identifier (e.g. `dependabot/...`) are silently dropped.
    """
    try:
        prs = github.list_pull_requests(owner, repo, state='open')
    except GithubError as exc:
        log.warning(
            'ci_redispatch: list_pull_requests(%s/%s) failed: %s', owner, repo, exc
        )
        return {}
    cache: dict[str, PullRequest] = {}
    for pr in prs:
        identifier = extract_linear_identifier(pr.head_branch)
        if identifier is None:
            continue
        cache.setdefault(identifier, pr)
    return cache


def _latest_completed_run(
    github: GithubClient, owner: str, repo: str, head_branch: str
) -> WorkflowRun | None:
    """Return the newest workflow run if it is completed, else None.

    The list endpoint orders by `created_at desc`, so `runs[0]` is the
    latest. A None return covers all the in-progress / queued / no-runs
    cases — including "an older run completed but a newer one is still
    running" — which the AC treats identically: skip silently, state
    untouched, wait for the in-flight run to finish.
    """
    try:
        runs = github.list_workflow_runs(owner, repo, head_branch)
    except GithubError as exc:
        log.warning(
            'ci_redispatch: list_workflow_runs(%s/%s@%s) failed: %s',
            owner,
            repo,
            head_branch,
            exc,
        )
        return None
    if not runs:
        return None
    latest = runs[0]
    if latest.status != 'completed':
        return None
    return latest


def _fire_for_failed_run(
    *,
    linear: LinearClient,
    github: GithubClient,
    issue: Issue,
    run: WorkflowRun,
    repo_owner: str,
    repo_name: str,
    in_progress_state_id: str,
) -> bool:
    """Post the failure comment, transition state, strip pickup-blocker labels.

    Returns True when the side-effects all landed and the caller should
    pin the run id in state. Any failure between subcalls leaves the
    state unchanged — the next tick re-evaluates from scratch and either
    retries the post or moves on if conditions changed.
    """
    try:
        jobs = github.list_workflow_jobs(repo_owner, repo_name, run.id)
    except GithubError as exc:
        log.warning(
            'ci_redispatch: list_workflow_jobs(%s/%s#%d) failed: %s',
            repo_owner,
            repo_name,
            run.id,
            exc,
        )
        return False
    failing_job = next((j for j in jobs if j.conclusion == 'failure'), None)
    if failing_job is None:
        log.warning(
            'ci_redispatch: run %d marked failure but no failing job found',
            run.id,
        )
        return False
    failing_step = next(
        (s for s in failing_job.steps if s.conclusion == 'failure'), None
    )
    step_name = failing_step.name if failing_step is not None else '<unknown>'
    try:
        raw_log = github.get_job_logs(repo_owner, repo_name, failing_job.id)
    except GithubError as exc:
        log.warning(
            'ci_redispatch: get_job_logs(%s/%s job=%d) failed: %s',
            repo_owner,
            repo_name,
            failing_job.id,
            exc,
        )
        raw_log = ''
    body = format_ci_failure_comment(
        workflow_name=run.name,
        run_url=run.html_url,
        job_name=failing_job.name,
        step_name=step_name,
        log_tail=truncate_log_tail(raw_log),
    )
    try:
        linear.add_comment(issue.id, body)
    except Exception as exc:
        log.warning('ci_redispatch: add_comment(%s) failed: %s', issue.identifier, exc)
        return False
    try:
        label_lookup = _team_labels_for_issue(linear, issue)
    except Exception as exc:
        log.warning(
            'ci_redispatch: label lookup for %s failed: %s', issue.identifier, exc
        )
        return False
    new_label_ids = _strip_pickup_blockers(issue.label_ids, label_lookup)
    update = IssueUpdate(
        state_id=in_progress_state_id,
        label_ids=new_label_ids,
        unset_assignee=True,
    )
    try:
        linear.update_issue(issue.id, update)
    except Exception as exc:
        log.warning('ci_redispatch: update_issue(%s) failed: %s', issue.identifier, exc)
        return False
    log.info(
        'ci_redispatch: %s %s -> %s on run %d',
        issue.identifier,
        issue.state_name,
        _IN_PROGRESS_STATE,
        run.id,
    )
    return True
