"""Worker loop and one-shot helpers.

`run_once` powers the Phase-2 `--once` mode (one issue, one CODER spawn).
`run_loop` is the Phase-3 main loop: poll Linear for unclaimed pickup-state
issues, claim with the two-level protocol (Linear assignee + remote branch
push), spawn claude, post-process Linear, repeat. Heartbeat is touched
once per iteration. SIGTERM/SIGINT triggers a graceful shutdown — the
current spawn is allowed to finish, but no new issues are picked up.
"""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import re
import signal
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from queue import Empty
from types import FrameType
from typing import Any, cast

from pydantic import SecretStr

from albedo.attachment_fetcher import (
    discover_attachments,
    fetch_attachments,
    format_attachments_block,
)
from albedo.claim import ClaimError, try_claim
from albedo.claim_manifest import (
    ClaimManifest,
    claim_manifest_path,
    clear_claim_manifest,
    write_claim_manifest,
)
from albedo.claude_runner import ClaudeRunResult, spawn_claude
from albedo.comment_filter import (
    filter_user_comments,
    format_user_comments_block,
)
from albedo.config import OrchestratorConfig
from albedo.dispatch import RoleSpec, UnknownColumnError, dispatch
from albedo.dispatch_messages import (
    ClaimedOk,
    ClaimLost,
    ResultMsg,
    TaskDone,
)
from albedo.heartbeat import heartbeat_path, touch_heartbeat
from albedo.linear_client import Comment, Issue, IssueUpdate, LinearClient
from albedo.prompt_builder import PromptBuilder, PromptContext
from albedo.status_writer import (
    PHASE_CLAIMING,
    PHASE_CLEANUP,
    PHASE_IN_PROGRESS,
    PHASE_POLLING,
    PHASE_POST_SPAWN,
    PHASE_RUNNING_CLAUDE,
    PHASE_SHUTDOWN,
    PHASE_SPAWNING_CLAUDE,
    StatusWriter,
)
from albedo.usage import UsageLedger, default_db_path
from albedo.worktree import WorktreeInfo, branch_for_issue, ensure_worktree

log = logging.getLogger(__name__)

ATTEMPTS_LABEL_PATTERN = re.compile(r'^attempts:(\d+)$')
PR_URL_PATTERN = re.compile(r'PR:\s*(https?://github\.com/[^\s/]+/[^\s/]+/pull/\d+)')
VERDICT_PATTERN = re.compile(
    r'^\s*VERDICT:\s*(APPROVE|REQUEST_CHANGES)\b', re.MULTILINE
)
DECOMPOSITION_HEADER = re.compile(r'^\s*DECOMPOSITION:\s*$', re.MULTILINE)
BLOCKED_PATTERN = re.compile(r'^\s*BLOCKED:\s*(.+?)\s*$', re.MULTILINE)
QUESTION_PATTERN = re.compile(r'^\s*QUESTION:\s*(.+?)\s*\Z', re.MULTILINE | re.DOTALL)
CANCEL_PATTERN = re.compile(r'^\s*CANCEL:\s*(.+?)\s*$', re.MULTILINE)
ISSUE_UPDATE_PATTERN = re.compile(
    r'^<<<ISSUE_UPDATE>>>\s*\n(.*?)\n^<<<END_ISSUE_UPDATE>>>\s*$',
    re.MULTILINE | re.DOTALL,
)
MAX_ATTEMPTS_BEFORE_ESCALATION = 3
MAX_CHILDREN = 7
ALLOWED_ESTIMATES = frozenset({1, 2, 3, 5, 8})


@dataclass(frozen=True, slots=True)
class RunOnceResult:
    issue: Issue
    role: RoleSpec
    worktree: WorktreeInfo
    claude: ClaudeRunResult
    pr_url: str | None = None
    linear_updated: bool = False


def parse_pr_url(text: str) -> str | None:
    """Find the first PR URL marker in claude's final response."""
    match = PR_URL_PATTERN.search(text)
    return match.group(1) if match else None


def parse_verdict(text: str) -> str | None:
    """Extract APPROVE / REQUEST_CHANGES from a `VERDICT:` line.

    Anything else (including `VERDICT: BLOCKED ...`) returns None — the
    caller treats absence as a blocker.
    """
    match = VERDICT_PATTERN.search(text)
    return match.group(1) if match else None


def parse_issue_update(text: str) -> str | None:
    """Extract the body of a `<<<ISSUE_UPDATE>>> ... <<<END_ISSUE_UPDATE>>>`
    block from agent output. Returns None when the marker is absent.
    """
    match = ISSUE_UPDATE_PATTERN.search(text)
    return match.group(1) if match else None


def find_pr_url_in_comments(comments: list[Comment]) -> str | None:
    """Scan Linear comments oldest-first for the Coder's `PR: <url>` line."""
    for comment in comments:
        url = parse_pr_url(comment.body)
        if url is not None:
            return url
    return None


def _build_attachments_block(
    *,
    config: OrchestratorConfig,
    linear: LinearClient,
    issue: Issue,
    comments: Sequence[Comment],
    worktree_path: Path,
) -> str:
    """Discover + download Linear attachments and render the prompt block.

    Returns an empty string when the feature is disabled or the issue
    carries nothing fetchable. Network/download failures are swallowed
    with a warning so a transient Linear hiccup never blocks the worker.
    """
    if not config.attachments.enabled:
        return ''
    items = discover_attachments(issue, comments)
    if not items:
        return ''
    try:
        fetched = fetch_attachments(
            items,
            dest_dir=worktree_path / '.linear-attachments' / issue.identifier,
            api_key=linear.api_key,
            limits=config.attachments,
        )
    except Exception as exc:
        log.warning('attachments: fetch for %s failed: %s', issue.identifier, exc)
        return ''
    return format_attachments_block(fetched, worktree_path)


_FINISHED_STATE_TYPES = frozenset({'completed', 'canceled'})


def is_blocked_by_incomplete(issue: Issue) -> bool:
    """True if any incoming `blocks` relation comes from an unfinished issue."""
    for rel in issue.incoming_relations:
        if rel.type == 'blocks' and rel.source_state_type not in _FINISHED_STATE_TYPES:
            return True
    return False


def filter_dispatchable(candidates: list[Issue]) -> list[Issue]:
    """Remove issues that workers must not pick up yet.

    Two cases are filtered:
      * `Triage` children — waiting on parent decomposition approval.
        Housekeeping releases them to Backlog when the parent is ready.
      * Issues with an incoming `blocks` relation from a not-yet-finished
        sibling — Architect records these via depends_on; workers must
        not start until the blocker reaches completed/canceled.
    """
    return [
        c
        for c in candidates
        if not (c.state_name == 'Triage' and c.parent_id is not None)
        and not is_blocked_by_incomplete(c)
    ]


def release_claim_assignee(*, linear: LinearClient, issue: Issue) -> None:
    """Drop the assignee on an issue, returning it to the pool.

    Mirrors `claim.release_claim` but exposed here so housekeeping can call
    it without importing claim.py (which knows about git internals).
    """
    linear.update_issue(issue.id, IssueUpdate(unset_assignee=True))


def build_mcp_extra_env(github_pat: SecretStr | None) -> dict[str, str]:
    """Build the env dict forwarded to `claude -p`.

    Process env wins (developer may have exported a token), then we fall back
    to whatever was loaded from `.env`. Empty values are dropped so MCP
    servers don't see literal empty strings.
    """
    forwarded: dict[str, str] = {}
    pat = os.environ.get('GITHUB_PERSONAL_ACCESS_TOKEN', '').strip()
    if not pat and github_pat is not None:
        pat = github_pat.get_secret_value().strip()
    if pat:
        forwarded['GITHUB_PERSONAL_ACCESS_TOKEN'] = pat
    return forwarded


def attempts_from_labels(label_names: tuple[str, ...]) -> int:
    """Read the highest `attempts:N` label as the current attempt count."""
    counts = [
        int(match.group(1))
        for label in label_names
        if (match := ATTEMPTS_LABEL_PATTERN.match(label))
    ]
    return max(counts) if counts else 0


def acceptance_criteria_from_description(description: str) -> tuple[str, ...]:
    """Pull `- ...` bullets that follow an "acceptance criteria" header.

    The convention §7.1 says Architect leaves an AC checklist in the issue
    description. We extract any markdown-style bullet (`-`, `*`, or `[ ]`)
    that lives under a heading mentioning "acceptance criteria" (case-insensitive).
    Falls back to an empty tuple when no header is present.
    """
    lines = description.splitlines()
    inside = False
    items: list[str] = []
    header = re.compile(r'^\s{0,3}#+\s.*acceptance\s+criteria', re.IGNORECASE)
    bullet = re.compile(r'^\s*(?:[-*]|\d+\.)\s+(?:\[[ x]\]\s+)?(.*\S)\s*$')
    for line in lines:
        if header.match(line):
            inside = True
            continue
        if inside:
            if line.strip().startswith('#'):
                break
            match = bullet.match(line)
            if match:
                items.append(match.group(1))
    return tuple(items)


PICKUP_STATES: tuple[str, ...] = ('Triage', 'Backlog', 'Review')
AWAITING_HUMAN_REPLY_LABEL = 'awaiting-human-reply'
CANCELLED_BY_HUMAN_LABEL = 'cancelled-by-human'
EXCLUDE_LABELS: tuple[str, ...] = (
    'blocked-external',
    'stuck',
    AWAITING_HUMAN_REPLY_LABEL,
    CANCELLED_BY_HUMAN_LABEL,
)


class _ShutdownFlag:
    """Tiny mutable holder so signal handlers can flip the loop flag."""

    def __init__(self) -> None:
        self.requested = False

    def request(self, _signum: int, _frame: FrameType | None) -> None:
        self.requested = True


def run_loop(
    *,
    config: OrchestratorConfig,
    linear: LinearClient,
    agent_id: str,
    agent_user_id: str,
    prompts_dir: Path,
    mcp_config_path: Path | None,
    dispatch_queue: mp.Queue,
    result_queue: mp.Queue,
    github_pat: SecretStr | None = None,
    cli: str = 'claude',
    install_signal_handlers: bool = True,
    bot_user_ids: frozenset[str] = frozenset(),
    display_name: str = '',
) -> None:
    """Main worker loop — consume `CandidateMsg` from the dispatch queue.

    The supervisor's poller is the sole producer; workers `get` from the
    queue, `try_claim` the issue (still per-agent so the Linear assignee
    is the worker's user), run the role, and `put` `ResultMsg` back so
    the supervisor can keep its in-flight set tight.

    A `None` on the dispatch queue is the shutdown sentinel. SIGTERM /
    SIGINT also wake the loop; in both cases the worker exits as soon
    as the current spawn (if any) finishes.
    """
    shutdown = _ShutdownFlag()
    if install_signal_handlers:
        signal.signal(signal.SIGTERM, shutdown.request)
        signal.signal(signal.SIGINT, shutdown.request)

    hb_path = heartbeat_path(config.state_dir, agent_id)
    ledger = UsageLedger(default_db_path(config.state_dir))
    status = StatusWriter(
        state_dir=config.state_dir, agent_id=agent_id, display_name=display_name
    )
    status.set_phase(PHASE_POLLING)

    log.info(
        'worker agent-%s started; consuming dispatch queue (timeout %.1fs)',
        agent_id,
        config.dispatch.poll_timeout_seconds,
    )

    poll_timeout = config.dispatch.poll_timeout_seconds

    while not shutdown.requested:
        touch_heartbeat(hb_path)

        try:
            msg = dispatch_queue.get(timeout=poll_timeout)
        except Empty:
            continue
        if msg is None:
            log.info('agent-%s received shutdown sentinel', agent_id)
            break

        issue = msg.issue
        status.set_phase(PHASE_CLAIMING)
        try:
            result = try_claim(linear=linear, issue=issue, agent_user_id=agent_user_id)
        except ClaimError as exc:
            log.warning('claim error on %s: %s', issue.identifier, exc)
            _post_result(result_queue, ClaimLost(issue.id))
            status.set_phase(PHASE_POLLING)
            continue
        if result is None:
            _post_result(result_queue, ClaimLost(issue.id))
            status.set_phase(PHASE_POLLING)
            continue

        claimed = result.issue
        _post_result(result_queue, ClaimedOk(claimed.id))
        log.info('claimed %s', claimed.identifier)
        try:
            _handle_claim_and_run(
                config=config,
                linear=linear,
                claimed=claimed,
                agent_id=agent_id,
                prompts_dir=prompts_dir,
                mcp_config_path=mcp_config_path,
                github_pat=github_pat,
                cli=cli,
                ledger=ledger,
                bot_user_ids=bot_user_ids,
                status=status,
            )
        finally:
            _post_result(result_queue, TaskDone(claimed.id))
            status.set_phase(PHASE_POLLING)

    status.set_phase(PHASE_SHUTDOWN)
    log.info('worker agent-%s shutdown complete', agent_id)


def _post_result(result_queue: mp.Queue, msg: ResultMsg) -> None:
    """Best-effort send to the supervisor's result queue.

    The supervisor drives `in_flight` from these messages, but the
    supervisor's TTL is the safety net — a dropped message leaks the
    in_flight slot for at most `offer_ttl_seconds`, never longer.
    """
    try:
        result_queue.put_nowait(msg)
    except Exception as exc:
        log.warning('failed to post result %s: %s', msg, exc)


def _handle_claim_and_run(
    *,
    config: OrchestratorConfig,
    linear: LinearClient,
    claimed: Issue,
    agent_id: str,
    prompts_dir: Path,
    mcp_config_path: Path | None,
    github_pat: SecretStr | None,
    cli: str,
    ledger: UsageLedger,
    bot_user_ids: frozenset[str],
    status: StatusWriter,
) -> None:
    """Drive a freshly-claimed issue through state transition + spawn.

    On any pre-spawn or spawn-time exception the claim is released back
    to the pool (assignee cleared, original state restored from manifest
    when present). The worker loop calls this exactly once per claimed
    issue and always emits a `TaskDone` after it returns.
    """
    try:
        claimed_role = dispatch(claimed.state_name)
        role_name = claimed_role.role
    except UnknownColumnError:
        role_name = ''
    status.set_issue(
        issue_id=claimed.id,
        identifier=claimed.identifier,
        title=claimed.title,
        role=role_name,
        url=claimed.url,
    )
    manifest: ClaimManifest | None = None
    try:
        states_map = _resolve_target_states(linear, claimed)
    except Exception as exc:
        log.warning('state lookup for %s failed: %s', claimed.identifier, exc)
        release_claim_assignee(linear=linear, issue=claimed)
        status.clear_issue()
        return
    try:
        status.set_phase(PHASE_IN_PROGRESS)
        manifest = _enter_in_progress(
            linear=linear,
            issue=claimed,
            states=states_map,
            state_dir=config.state_dir,
            agent_id=agent_id,
        )
    except Exception as exc:
        log.warning('in-progress transition for %s failed: %s', claimed.identifier, exc)
        release_claim_assignee(linear=linear, issue=claimed)
        status.clear_issue()
        return

    try:
        status.set_phase(PHASE_SPAWNING_CLAUDE)
        run_once_result = run_claimed(
            config=config,
            linear=linear,
            issue=claimed,
            agent_id=agent_id,
            prompts_dir=prompts_dir,
            mcp_config_path=mcp_config_path,
            github_pat=github_pat,
            cli=cli,
            usage_ledger=ledger,
            bot_user_ids=bot_user_ids,
            status_writer=status,
        )
    except UnknownColumnError as exc:
        log.warning(
            'no role for %s in column %r: %s',
            claimed.identifier,
            claimed.state_name,
            exc,
        )
        if manifest is not None:
            _restore_claimed_state(
                linear=linear,
                manifest=manifest,
                state_dir=config.state_dir,
                agent_id=agent_id,
            )
        release_claim_assignee(linear=linear, issue=claimed)
        status.clear_issue()
        return
    except Exception as exc:
        log.exception('run_once for %s crashed: %s', claimed.identifier, exc)
        if manifest is not None:
            _restore_claimed_state(
                linear=linear,
                manifest=manifest,
                state_dir=config.state_dir,
                agent_id=agent_id,
            )
        release_claim_assignee(linear=linear, issue=claimed)
        status.clear_issue()
        return

    status.set_phase(PHASE_CLEANUP)
    clear_claim_manifest(claim_manifest_path(config.state_dir, agent_id))

    if run_once_result.claude.is_error:
        log.info(
            '%s blocked: %s',
            claimed.identifier,
            run_once_result.claude.result_text[:200],
        )
        status.note(f'blocked: {run_once_result.claude.result_text[:120]}')
    else:
        status.note(f'completed {claimed.identifier}')
    status.clear_issue()


def _outcome_label(claude: ClaudeRunResult) -> str:
    """Coarse outcome bucket for the usage ledger and TUI counters."""
    if claude.timed_out:
        return 'timeout'
    if claude.is_error:
        return 'fail'
    return 'done'


def run_claimed(
    *,
    config: OrchestratorConfig,
    linear: LinearClient,
    issue: Issue,
    agent_id: str,
    prompts_dir: Path,
    mcp_config_path: Path | None,
    github_pat: SecretStr | None,
    cli: str,
    usage_ledger: UsageLedger | None = None,
    bot_user_ids: frozenset[str] = frozenset(),
    status_writer: StatusWriter | None = None,
) -> RunOnceResult:
    """Execute the per-task pipeline for an already-claimed issue.

    Mirrors `run_once` but skips the initial `linear.get_issue` (caller has
    a fresh `Issue` from the claim path) and skips dispatch errors so the
    caller can decide how to release.
    """
    role = dispatch(issue.state_name)

    github = config.repo.github
    repo_name = github.repo if github else config.repo.path.name

    worktree = ensure_worktree(
        config.repo.path,
        config.worktree_root,
        repo_name,
        issue.identifier,
        config.repo.base_branch,
    )
    if status_writer is not None:
        status_writer.set_issue(
            issue_id=issue.id,
            identifier=issue.identifier,
            title=issue.title,
            role=role.role,
            worktree=str(worktree.path),
            branch=branch_for_issue(issue.identifier),
            url=issue.url,
        )

    pr_url_for_prompt: str | None = None
    raw_comments: list[Comment] | None = None
    if role.role == 'REVIEWER':
        try:
            raw_comments = linear.list_comments(issue.id)
        except Exception as exc:
            log.warning('list_comments for %s failed: %s', issue.identifier, exc)
            raw_comments = []
        pr_url_for_prompt = find_pr_url_in_comments(raw_comments)

    if (
        config.features.user_comments_in_prompt or config.attachments.enabled
    ) and raw_comments is None:
        try:
            raw_comments = linear.list_comments(issue.id)
        except Exception as exc:
            log.warning('list_comments for %s failed: %s', issue.identifier, exc)
            raw_comments = []

    if config.features.user_comments_in_prompt:
        user_comments_block = format_user_comments_block(
            filter_user_comments(raw_comments or [], bot_user_ids)
        )
    else:
        user_comments_block = ''

    attachments_block = _build_attachments_block(
        config=config,
        linear=linear,
        issue=issue,
        comments=raw_comments or [],
        worktree_path=worktree.path,
    )

    builder = PromptBuilder(prompts_dir)
    context = PromptContext(
        agent_id=agent_id,
        issue_id=issue.identifier,
        title=issue.title,
        description=issue.description,
        acceptance_criteria=acceptance_criteria_from_description(issue.description),
        column=issue.state_name,
        role=role.role,
        worktree_path=str(worktree.path),
        branch=branch_for_issue(issue.identifier),
        base_branch=config.repo.base_branch,
        repo_name=repo_name,
        allowed_tools=','.join(role.allowed_tools),
        target_column_on_success=role.target_state_on_success,
        target_column_on_blocker=role.target_state_on_blocker,
        role_timeout_minutes=role.timeout_minutes,
        parent_id=issue.parent_id,
        attempts=attempts_from_labels(issue.label_names),
        pr_url=pr_url_for_prompt,
        user_comments_block=user_comments_block,
        attachments_block=attachments_block,
    )
    prompt = builder.build(role.prompt_template, context)

    transcript_dir = config.state_dir / 'transcripts'
    on_event = None
    if status_writer is not None:
        from albedo.stream_parser import StreamSnapshot

        # Mutable closure flag — flips once we've seen the first non-system
        # event, signalling that claude finished booting (auth, MCP init,
        # cache warm-up) and is actually doing work. We then bump the phase
        # without resetting the elapsed clock so the user sees a continuous
        # "time on this task" counter.
        transitioned = [False]

        def _publish(event: object, snapshot: object) -> None:
            if not transitioned[0] and isinstance(event, dict):
                event_dict = cast('dict[str, Any]', event)
                if event_dict.get('type') == 'assistant':
                    status_writer.set_phase(PHASE_RUNNING_CLAUDE, reset_clock=False)
                    transitioned[0] = True
            if isinstance(snapshot, StreamSnapshot):
                status_writer.update_stream(snapshot)

        on_event = _publish

    log.info('%s [%s] spawning claude', issue.identifier, role.role)
    spawn_started_at = time.time()
    claude = spawn_claude(
        prompt,
        cwd=worktree.path,
        allowed_tools=list(role.allowed_tools),
        mcp_config_path=mcp_config_path,
        max_turns=role.max_turns,
        timeout_seconds=role.timeout_minutes * 60,
        cli=cli,
        extra_env=build_mcp_extra_env(github_pat),
        transcript_dir=transcript_dir,
        transcript_basename=issue.identifier,
        on_event=on_event,
        permission_mode=role.permission_mode,
        model=config.models.for_role(role.role),
    )
    wallclock_sec = max(0, int(time.time() - spawn_started_at))
    outcome = _outcome_label(claude)
    log.info(
        '%s [%s] claude done in %ds (%s, cost=$%.3f)',
        issue.identifier,
        role.role,
        wallclock_sec,
        outcome,
        claude.total_cost_usd or 0.0,
    )
    if usage_ledger is not None:
        try:
            usage_ledger.record_usage(
                agent_id=agent_id,
                issue_id=issue.identifier,
                usage=claude.usage,
                outcome=outcome,
                wallclock_sec=wallclock_sec,
                cost_usd=claude.total_cost_usd,
                role=role.role,
            )
        except Exception as exc:
            log.warning('usage ledger record failed: %s', exc)
    if status_writer is not None:
        status_writer.set_phase(PHASE_POST_SPAWN)
    del wallclock_sec, outcome  # already persisted; locals retained for clarity
    pr_url, linear_updated = _post_spawn_linear_update(
        linear=linear,
        issue=issue,
        role=role,
        claude=claude,
        states=_resolve_target_states(linear, issue),
        agent_id=agent_id,
        enable_body_edits=config.features.agent_body_edits,
        max_attempts_before_escalation=config.max_attempts_before_escalation,
    )
    return RunOnceResult(
        issue=issue,
        role=role,
        worktree=worktree,
        claude=claude,
        pr_url=pr_url,
        linear_updated=linear_updated,
    )


def run_once(
    *,
    config: OrchestratorConfig,
    linear: LinearClient,
    issue_identifier: str,
    agent_id: str,
    prompts_dir: Path,
    mcp_config_path: Path | None,
    cli: str = 'claude',
    fetch: bool = True,
    github_pat: SecretStr | None = None,
    bot_user_ids: frozenset[str] = frozenset(),
) -> RunOnceResult:
    """Run a single CODER iteration on an existing Linear issue.

    After `spawn_claude` returns, parse the `PR: <url>` marker, post the
    Linear comment, and move the issue to the role's success column. On
    failure (claude error or no PR URL) the issue is moved to the blocker
    column with a diagnostic comment.
    """
    issue = linear.get_issue(issue_identifier)
    role = dispatch(issue.state_name)

    github = config.repo.github
    repo_name = github.repo if github else config.repo.path.name

    worktree = ensure_worktree(
        config.repo.path,
        config.worktree_root,
        repo_name,
        issue.identifier,
        config.repo.base_branch,
        fetch=fetch,
    )

    raw_comments: list[Comment] = []
    if config.features.user_comments_in_prompt or config.attachments.enabled:
        try:
            raw_comments = linear.list_comments(issue.id)
        except Exception as exc:
            log.warning('list_comments for %s failed: %s', issue.identifier, exc)
            raw_comments = []

    if config.features.user_comments_in_prompt:
        user_comments_block = format_user_comments_block(
            filter_user_comments(raw_comments, bot_user_ids)
        )
    else:
        user_comments_block = ''

    attachments_block = _build_attachments_block(
        config=config,
        linear=linear,
        issue=issue,
        comments=raw_comments,
        worktree_path=worktree.path,
    )

    builder = PromptBuilder(prompts_dir)
    context = PromptContext(
        agent_id=agent_id,
        issue_id=issue.identifier,
        title=issue.title,
        description=issue.description,
        acceptance_criteria=acceptance_criteria_from_description(issue.description),
        column=issue.state_name,
        role=role.role,
        worktree_path=str(worktree.path),
        branch=branch_for_issue(issue.identifier),
        base_branch=config.repo.base_branch,
        repo_name=repo_name,
        allowed_tools=','.join(role.allowed_tools),
        target_column_on_success=role.target_state_on_success,
        target_column_on_blocker=role.target_state_on_blocker,
        role_timeout_minutes=role.timeout_minutes,
        parent_id=issue.parent_id,
        attempts=attempts_from_labels(issue.label_names),
        user_comments_block=user_comments_block,
        attachments_block=attachments_block,
    )
    prompt = builder.build(role.prompt_template, context)

    claude = spawn_claude(
        prompt,
        cwd=worktree.path,
        allowed_tools=list(role.allowed_tools),
        mcp_config_path=mcp_config_path,
        max_turns=role.max_turns,
        timeout_seconds=role.timeout_minutes * 60,
        cli=cli,
        extra_env=build_mcp_extra_env(github_pat),
        transcript_dir=config.state_dir / 'transcripts',
        transcript_basename=issue.identifier,
        permission_mode=role.permission_mode,
        model=config.models.for_role(role.role),
    )

    pr_url, linear_updated = _post_spawn_linear_update(
        linear=linear,
        issue=issue,
        role=role,
        claude=claude,
        states=_resolve_target_states(linear, issue),
        agent_id=agent_id,
        enable_body_edits=config.features.agent_body_edits,
        max_attempts_before_escalation=config.max_attempts_before_escalation,
    )

    return RunOnceResult(
        issue=issue,
        role=role,
        worktree=worktree,
        claude=claude,
        pr_url=pr_url,
        linear_updated=linear_updated,
    )


IN_PROGRESS_STATE_NAME = 'In Progress'


def _enter_in_progress(
    *,
    linear: LinearClient,
    issue: Issue,
    states: dict[str, str],
    state_dir: Path,
    agent_id: str,
) -> ClaimManifest | None:
    """Move a freshly-claimed issue to `In Progress` and persist a manifest.

    The manifest captures the prior `state_id`/`state_name` so stale-claim
    recovery can restore it if the worker crashes before the post-spawn
    handler moves the issue onward. If the team has no `In Progress`
    state (legacy setup), this is a no-op and returns `None`.
    """
    in_progress_id = states.get(IN_PROGRESS_STATE_NAME)
    if in_progress_id is None:
        log.warning(
            "team has no '%s' state — skipping in-progress transition for %s",
            IN_PROGRESS_STATE_NAME,
            issue.identifier,
        )
        return None
    manifest = ClaimManifest(
        issue_id=issue.id,
        issue_identifier=issue.identifier,
        prev_state_id=issue.state_id,
        prev_state_name=issue.state_name,
        claimed_at_unix=time.time(),
    )
    write_claim_manifest(claim_manifest_path(state_dir, agent_id), manifest)
    linear.update_issue(issue.id, IssueUpdate(state_id=in_progress_id))
    log.info('%s -> In Progress', issue.identifier)
    return manifest


def _restore_claimed_state(
    *,
    linear: LinearClient,
    manifest: ClaimManifest,
    state_dir: Path,
    agent_id: str,
) -> None:
    """Best-effort: move the issue back to its pre-claim state."""
    try:
        linear.update_issue(
            manifest.issue_id, IssueUpdate(state_id=manifest.prev_state_id)
        )
    except Exception as exc:
        log.warning(
            'failed to restore state for %s: %s', manifest.issue_identifier, exc
        )
    clear_claim_manifest(claim_manifest_path(state_dir, agent_id))


def _resolve_target_states(linear: LinearClient, issue: Issue) -> dict[str, str]:
    """Map state name → state_id for the issue's team using one query.

    The dispatch table refers to states by name; Linear's update API requires
    UUIDs. We fetch the team's state list once per spawn (cheap) and use it
    for both success and blocker moves.
    """
    document = """
        query StatesForIssue($id: String!) {
          issue(id: $id) {
            team {
              states { nodes { id name } }
            }
          }
        }
    """
    data = linear.query(document, {'id': issue.identifier})
    nodes = data['issue']['team']['states']['nodes']
    return {n['name']: n['id'] for n in nodes}


def _post_spawn_linear_update(
    *,
    linear: LinearClient,
    issue: Issue,
    role: RoleSpec,
    claude: ClaudeRunResult,
    states: dict[str, str],
    agent_id: str,
    enable_body_edits: bool = True,
    max_attempts_before_escalation: int = MAX_ATTEMPTS_BEFORE_ESCALATION,
) -> tuple[str | None, bool]:
    """Post-spawn Linear updates dispatched by role.

    Comments are prefixed with `**agent-N**:` so author intent is visible
    even when all workers share a Linear user. Each role's verdict is
    parsed from the marker line claude is required to print.

    `<<<ISSUE_UPDATE>>>` blocks (if any) are applied before role-specific
    dispatch so downstream parsers see the same `claude.result_text` they
    always have, and the user-comment loop sees the rewritten body on the
    next pickup.
    """
    if enable_body_edits:
        apply_issue_update_marker(
            linear=linear,
            issue=issue,
            claude=claude,
            agent_id=agent_id,
        )
    if role.role == 'REVIEWER':
        return _post_spawn_reviewer(
            linear=linear,
            issue=issue,
            role=role,
            claude=claude,
            states=states,
            agent_id=agent_id,
            max_attempts_before_escalation=max_attempts_before_escalation,
        )
    if role.role == 'ARCHITECT':
        return _post_spawn_architect(
            linear=linear,
            issue=issue,
            role=role,
            claude=claude,
            states=states,
            agent_id=agent_id,
        )
    return _post_spawn_coder(
        linear=linear,
        issue=issue,
        role=role,
        claude=claude,
        states=states,
        agent_id=agent_id,
    )


def apply_issue_update_marker(
    *,
    linear: LinearClient,
    issue: Issue,
    claude: ClaudeRunResult,
    agent_id: str,
) -> bool:
    """Apply `<<<ISSUE_UPDATE>>>` block from agent output if present.

    The previous body is preserved as a Linear comment inside a `<details>`
    fold (Linear has no body audit log) so reviewers and `git blame`-style
    investigations can still see what the issue used to say. Returns True
    when an update was applied, False otherwise.
    """
    new_body = parse_issue_update(claude.result_text or '')
    if new_body is None:
        return False
    new_body = new_body.strip()
    if not new_body:
        log.warning(
            'agent-%s emitted ISSUE_UPDATE marker with empty body for %s — ignoring',
            agent_id,
            issue.identifier,
        )
        return False
    if new_body == (issue.description or '').strip():
        return False
    prefix = f'**agent-{agent_id}**: '
    audit_body = (
        f'{prefix}ISSUE_BODY_UPDATED\n\n'
        f'<details><summary>previous body</summary>\n\n'
        f'{issue.description or "(empty)"}\n\n'
        f'</details>'
    )
    try:
        linear.add_comment(issue.id, audit_body)
        linear.update_issue(issue.id, IssueUpdate(description=new_body))
    except Exception as exc:
        log.warning(
            'agent-%s ISSUE_UPDATE for %s failed: %s', agent_id, issue.identifier, exc
        )
        return False
    log.info('agent-%s rewrote description of %s', agent_id, issue.identifier)
    return True


def _post_spawn_coder(
    *,
    linear: LinearClient,
    issue: Issue,
    role: RoleSpec,
    claude: ClaudeRunResult,
    states: dict[str, str],
    agent_id: str,
) -> tuple[str | None, bool]:
    pr_url = parse_pr_url(claude.result_text)
    prefix = f'**agent-{agent_id}**: '

    if claude.is_error or pr_url is None:
        reason = (
            'claude reported an error'
            if claude.is_error
            else 'no PR URL marker found in claude output'
        )
        body = f'{prefix}BLOCKED: {reason}.\n\n```\n{claude.result_text[:1500]}\n```'
        linear.add_comment(issue.id, body)
        target = states.get(role.target_state_on_blocker)
        if target is None:
            return pr_url, False
        # If the agent's output explicitly carried a `BLOCKED: ...` line,
        # treat it as a clarifying question to the human and gate the
        # issue from re-pickup until a new user comment arrives. Pure
        # error/no-marker cases stay re-pickable so transient failures
        # self-heal.
        update_labels = issue.label_ids
        if not claude.is_error and BLOCKED_PATTERN.search(claude.result_text or ''):
            label_lookup = _resolve_team_labels(linear, issue)
            update_labels = _add_label(
                update_labels, label_lookup, AWAITING_HUMAN_REPLY_LABEL
            )
        linear.update_issue(
            issue.id,
            IssueUpdate(
                state_id=target,
                label_ids=update_labels,
                unset_assignee=True,
            ),
        )
        log.info(
            '%s -> %s (coder blocked)',
            issue.identifier,
            role.target_state_on_blocker,
        )
        return pr_url, True

    linear.add_comment(issue.id, f'{prefix}PR: {pr_url}')
    target = states.get(role.target_state_on_success)
    if target is None:
        return pr_url, False
    # Unset assignee on success so the next role (Reviewer) can claim the
    # issue. `list_pickup_issues` filters by `assignee = null`; without this
    # step the issue would sit in Review forever with the Coder's assignee.
    linear.update_issue(issue.id, IssueUpdate(state_id=target, unset_assignee=True))
    log.info(
        '%s -> %s; PR %s',
        issue.identifier,
        role.target_state_on_success,
        pr_url,
    )
    return pr_url, True


def _post_spawn_reviewer(
    *,
    linear: LinearClient,
    issue: Issue,
    role: RoleSpec,
    claude: ClaudeRunResult,
    states: dict[str, str],
    agent_id: str,
    max_attempts_before_escalation: int = MAX_ATTEMPTS_BEFORE_ESCALATION,
) -> tuple[str | None, bool]:
    """Reviewer outcomes: APPROVE / REQUEST_CHANGES / blocked.

    APPROVE → state Awaiting approval, label `kind:final-pr` added.
    REQUEST_CHANGES → attempts:N label incremented, assignee unset, state
    Backlog. After hitting `max_attempts_before_escalation`, the issue is
    escalated instead: state Awaiting approval with `stuck` label.
    Anything else (claude error, missing or BLOCKED verdict) is treated as
    a blocker — issue moves to Backlog with a diagnostic comment, and
    attempts are NOT incremented (we couldn't even verify the PR).
    """
    prefix = f'**agent-{agent_id}**: '
    summary = claude.result_text or ''
    verdict = parse_verdict(summary)
    label_lookup = _resolve_team_labels(linear, issue)

    if claude.is_error or verdict is None:
        reason = (
            'reviewer claude reported an error'
            if claude.is_error
            else 'no VERDICT: APPROVE/REQUEST_CHANGES marker in reviewer output'
        )
        body = f'{prefix}BLOCKED: {reason}.\n\n```\n{summary[:1500]}\n```'
        linear.add_comment(issue.id, body)
        target = states.get(role.target_state_on_blocker)
        if target is None:
            return None, False
        linear.update_issue(issue.id, IssueUpdate(state_id=target, unset_assignee=True))
        log.info(
            '%s -> %s (reviewer blocked)',
            issue.identifier,
            role.target_state_on_blocker,
        )
        return None, True

    if verdict == 'APPROVE':
        body = f'{prefix}REVIEW APPROVE\n\n{summary[:2000]}'
        linear.add_comment(issue.id, body)
        new_labels = _add_label(issue.label_ids, label_lookup, 'kind:final-pr')
        target = states.get(role.target_state_on_success)
        if target is None:
            return None, False
        linear.update_issue(
            issue.id, IssueUpdate(state_id=target, label_ids=new_labels)
        )
        log.info('%s APPROVE -> %s', issue.identifier, role.target_state_on_success)
        return None, True

    # REQUEST_CHANGES path.
    current_attempts = attempts_from_labels(issue.label_names)
    next_attempts = current_attempts + 1

    if next_attempts >= max_attempts_before_escalation:
        new_labels = _set_attempts_label(issue.label_ids, label_lookup, next_attempts)
        new_labels = _add_label(new_labels, label_lookup, 'stuck')
        body = (
            f'{prefix}REVIEW REQUEST_CHANGES (attempts={next_attempts}) — '
            f'escalating to human.\n\n{summary[:2000]}'
        )
        linear.add_comment(issue.id, body)
        target = states.get('Awaiting approval')
        if target is None:
            return None, False
        linear.update_issue(
            issue.id, IssueUpdate(state_id=target, label_ids=new_labels)
        )
        log.info(
            '%s REQUEST_CHANGES (attempts=%d) -> Awaiting approval (stuck)',
            issue.identifier,
            next_attempts,
        )
        return None, True

    new_labels = _set_attempts_label(issue.label_ids, label_lookup, next_attempts)
    body = (
        f'{prefix}REVIEW REQUEST_CHANGES (attempts={next_attempts}/'
        f'{max_attempts_before_escalation}).\n\n{summary[:2000]}'
    )
    linear.add_comment(issue.id, body)
    target = states.get(role.target_state_on_blocker)
    if target is None:
        return None, False
    linear.update_issue(
        issue.id,
        IssueUpdate(
            state_id=target,
            label_ids=new_labels,
            unset_assignee=True,
        ),
    )
    log.info(
        '%s REQUEST_CHANGES (attempts=%d/%d) -> %s',
        issue.identifier,
        next_attempts,
        max_attempts_before_escalation,
        role.target_state_on_blocker,
    )
    return None, True


@dataclass(frozen=True, slots=True)
class ChildSpec:
    title: str
    description: str
    acceptance_criteria: tuple[str, ...]
    estimate: int
    depends_on: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Decomposition:
    rationale: str
    children: tuple[ChildSpec, ...]


class DecompositionParseError(ValueError):
    """Raised when the ARCHITECT response is malformed JSON or shape."""


def parse_decomposition(text: str) -> Decomposition:
    """Extract `DECOMPOSITION:` JSON block and validate shape.

    Validates: 2..MAX_CHILDREN children; each has title, description,
    acceptance_criteria (≥1 item), estimate in {1,2,3,5,8}.
    """
    header = DECOMPOSITION_HEADER.search(text)
    if header is None:
        raise DecompositionParseError('no DECOMPOSITION: header in architect output')
    body = text[header.end() :]
    json_text = _strip_code_fence(body).strip()
    if not json_text:
        raise DecompositionParseError('empty body after DECOMPOSITION:')
    try:
        payload_raw: object = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise DecompositionParseError(f'invalid JSON: {exc}') from exc
    if not isinstance(payload_raw, dict):
        raise DecompositionParseError('decomposition root must be object')
    payload = cast('dict[str, Any]', payload_raw)

    raw_children_obj = payload.get('children')
    if not isinstance(raw_children_obj, list):
        raise DecompositionParseError('children must be a list')
    raw_children = cast('list[Any]', raw_children_obj)
    if not 2 <= len(raw_children) <= MAX_CHILDREN:
        raise DecompositionParseError(
            f'children length must be 2..{MAX_CHILDREN}, got {len(raw_children)}'
        )

    children: list[ChildSpec] = []
    for idx, raw in enumerate(raw_children):
        children.append(_validate_child_spec(idx, raw))

    # Cross-child validation: every depends_on must be a backwards index.
    for idx, child in enumerate(children):
        for dep in child.depends_on:
            if dep < 0 or dep >= len(children):
                raise DecompositionParseError(
                    f'child[{idx}].depends_on[{dep}] out of range '
                    f'(must be 0..{len(children) - 1})'
                )
            if dep == idx:
                raise DecompositionParseError(
                    f'child[{idx}].depends_on contains self-reference'
                )
            if dep > idx:
                raise DecompositionParseError(
                    f'child[{idx}].depends_on[{dep}] points forward; '
                    'only backward references allowed'
                )

    rationale_raw = payload.get('rationale', '')
    rationale = str(rationale_raw) if rationale_raw is not None else ''
    return Decomposition(rationale=rationale, children=tuple(children))


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith('```'):
        return stripped
    # Remove the opening fence (and optional language tag) and the
    # closing fence if present.
    first_newline = stripped.find('\n')
    if first_newline == -1:
        return ''
    inner = stripped[first_newline + 1 :]
    if inner.rstrip().endswith('```'):
        inner = inner.rstrip()[:-3]
    return inner.strip()


def _validate_child_spec(idx: int, raw: object) -> ChildSpec:
    if not isinstance(raw, dict):
        raise DecompositionParseError(f'child[{idx}] must be an object')
    raw_dict: dict[str, Any] = raw  # type: ignore[assignment]
    title = raw_dict.get('title')
    description = raw_dict.get('description')
    ac = raw_dict.get('acceptance_criteria')
    estimate = raw_dict.get('estimate')
    if not isinstance(title, str) or not title.strip():
        raise DecompositionParseError(f'child[{idx}].title must be non-empty string')
    if not isinstance(description, str) or not description.strip():
        raise DecompositionParseError(
            f'child[{idx}].description must be non-empty string'
        )
    if not isinstance(ac, list) or not ac:
        raise DecompositionParseError(
            f'child[{idx}].acceptance_criteria must be non-empty list'
        )
    ac_list = cast('list[Any]', ac)
    ac_strs: list[str] = []
    for j, item in enumerate(ac_list):
        if not isinstance(item, str) or not item.strip():
            raise DecompositionParseError(
                f'child[{idx}].acceptance_criteria[{j}] must be non-empty string'
            )
        ac_strs.append(item.strip())
    if not isinstance(estimate, int) or estimate not in ALLOWED_ESTIMATES:
        raise DecompositionParseError(
            f'child[{idx}].estimate must be one of '
            f'{sorted(ALLOWED_ESTIMATES)}, got {estimate!r}'
        )
    depends_on_raw = raw_dict.get('depends_on', [])
    if not isinstance(depends_on_raw, list):
        raise DecompositionParseError(
            f'child[{idx}].depends_on must be a list (or omitted)'
        )
    deps_list = cast('list[Any]', depends_on_raw)
    deps_ints: list[int] = []
    for j, dep in enumerate(deps_list):
        if not isinstance(dep, int) or isinstance(dep, bool):
            raise DecompositionParseError(
                f'child[{idx}].depends_on[{j}] must be an int index'
            )
        deps_ints.append(dep)
    return ChildSpec(
        title=title.strip(),
        description=description.strip(),
        acceptance_criteria=tuple(ac_strs),
        estimate=estimate,
        depends_on=tuple(deps_ints),
    )


def _post_spawn_architect(
    *,
    linear: LinearClient,
    issue: Issue,
    role: RoleSpec,
    claude: ClaudeRunResult,
    states: dict[str, str],
    agent_id: str,
) -> tuple[str | None, bool]:
    """ARCHITECT outcomes: decomposition, ambiguous question, or epic split.

    Successful decomposition: pre-existing children archived, new children
    created with `draft` + `attempts:0` invariants, parent moved to
    `Awaiting approval` with `kind:decomposition` label and a rationale
    comment listing all children.

    Ambiguous (`BLOCKED: ...`): question posted as comment, parent stays
    in Triage. "needs epic split" additionally adds `blocked-external`
    so workers stop polling it.
    """
    prefix = f'**agent-{agent_id}**: '
    summary = (claude.result_text or '').strip()
    label_lookup = _resolve_team_labels(linear, issue)

    if claude.is_error:
        body = (
            f'{prefix}BLOCKED: architect claude reported an error.\n\n'
            f'```\n{summary[:1500]}\n```'
        )
        linear.add_comment(issue.id, body)
        target = states.get(role.target_state_on_blocker)
        if target is None:
            return None, False
        linear.update_issue(issue.id, IssueUpdate(state_id=target, unset_assignee=True))
        log.info(
            '%s -> %s (architect blocked)',
            issue.identifier,
            role.target_state_on_blocker,
        )
        return None, True

    cancel = CANCEL_PATTERN.search(summary)
    if cancel is not None:
        reason = cancel.group(1).strip()
        new_labels = _remove_label(
            issue.label_ids, label_lookup, AWAITING_HUMAN_REPLY_LABEL
        )
        body = f'{prefix}CANCELLED: {reason}'
        linear.add_comment(issue.id, body)
        target = states.get('Canceled')
        if target is None:
            log.warning(
                'Canceled state missing on team; falling back to %s',
                role.target_state_on_blocker,
            )
            target = states.get(role.target_state_on_blocker)
            if target is None:
                return None, False
        linear.update_issue(
            issue.id,
            IssueUpdate(
                state_id=target,
                unset_assignee=True,
                label_ids=new_labels,
            ),
        )
        log.info('%s CANCELLED: %s', issue.identifier, reason[:80])
        return None, True

    question = QUESTION_PATTERN.search(summary)
    if question is not None:
        text = question.group(1).strip()
        new_labels = _add_label(
            issue.label_ids, label_lookup, AWAITING_HUMAN_REPLY_LABEL
        )
        # The `QUESTION:` token is a parser marker, not human-facing —
        # the `**agent-N**:` prefix already signals "this is from the bot".
        body = f'{prefix}{text}'
        linear.add_comment(issue.id, body)
        target = states.get(role.target_state_on_blocker)
        if target is None:
            return None, False
        linear.update_issue(
            issue.id,
            IssueUpdate(
                state_id=target,
                unset_assignee=True,
                label_ids=new_labels,
            ),
        )
        log.info(
            '%s QUESTION -> %s: %s',
            issue.identifier,
            role.target_state_on_blocker,
            text[:80],
        )
        return None, True

    blocked = BLOCKED_PATTERN.search(summary)
    if blocked is not None:
        reason = blocked.group(1).strip()
        new_labels = issue.label_ids
        if 'epic split' in reason.lower():
            new_labels = _add_label(new_labels, label_lookup, 'blocked-external')
        else:
            # Defensive fallback: agent emitted legacy BLOCKED for what
            # should now be a QUESTION. Treat as awaiting-human-reply so
            # the redispatch loop still works, and warn so we notice.
            log.warning(
                '%s emitted legacy BLOCKED for clarifying question; '
                'agent should use QUESTION: marker',
                issue.identifier,
            )
            new_labels = _add_label(
                new_labels, label_lookup, AWAITING_HUMAN_REPLY_LABEL
            )
        body = f'{prefix}BLOCKED: {reason}'
        linear.add_comment(issue.id, body)
        target = states.get(role.target_state_on_blocker)
        if target is None:
            return None, False
        linear.update_issue(
            issue.id,
            IssueUpdate(
                state_id=target,
                unset_assignee=True,
                label_ids=new_labels,
            ),
        )
        log.info(
            '%s BLOCKED -> %s: %s',
            issue.identifier,
            role.target_state_on_blocker,
            reason[:80],
        )
        return None, True

    try:
        decomposition = parse_decomposition(summary)
    except DecompositionParseError as exc:
        body = (
            f'{prefix}BLOCKED: architect output failed to parse — {exc}.\n\n'
            f'```\n{summary[:1500]}\n```'
        )
        linear.add_comment(issue.id, body)
        target = states.get(role.target_state_on_blocker)
        if target is None:
            return None, False
        linear.update_issue(issue.id, IssueUpdate(state_id=target, unset_assignee=True))
        log.info(
            '%s -> %s (architect parse error)',
            issue.identifier,
            role.target_state_on_blocker,
        )
        return None, True

    # Pre-flight: archive any pre-existing children (rejected previous
    # decomposition pass).
    existing = linear.list_children(issue.id)
    archived_ids: list[str] = []
    for child in existing:
        linear.archive_issue(child.id)
        archived_ids.append(child.identifier)

    # Resolve team_id from the issue's parent state lookup. Easier path:
    # use the issue's existing team — we resolve via a quick query.
    team_id = _resolve_team_id_for_issue(linear, issue)

    # Children sit in Triage until the human approves the parent
    # decomposition (Variant A). The worker loop skips Triage issues with
    # a non-null parent_id, so they're effectively gated by state, not by
    # a custom `draft` label.
    triage_state_id = states.get('Triage')

    project_id = _resolve_project_id_for_issue(linear, issue)

    created: list[Issue] = []
    for spec in decomposition.children:
        description = _format_child_description(
            spec, parent_identifier=issue.identifier
        )
        child = linear.create_issue(
            team_id=team_id,
            title=spec.title,
            description=description,
            parent_id=issue.id,
            estimate=spec.estimate,
            state_id=triage_state_id,
            project_id=project_id,
        )
        created.append(child)

    # After all children exist, encode declared dependencies as Linear
    # `blocks` relations. Linear's relation semantics are
    # "issueId blocks relatedIssueId" — so the BLOCKER is the first arg
    # and the DEPENDENT is the second. `depends_on` is a list of
    # blockers (backwards-only by validation), so for each (idx, dep)
    # pair we want `created[dep]` to block `created[idx]`.
    for idx, spec in enumerate(decomposition.children):
        for dep in spec.depends_on:
            try:
                linear.create_issue_relation(
                    created[dep].id,  # blocker
                    created[idx].id,  # dependent
                    relation_type='blocks',
                )
            except Exception as exc:
                log.warning(
                    'failed to create blocks relation %s -> %s: %s',
                    created[dep].identifier,
                    created[idx].identifier,
                    exc,
                )

    rationale_lines: list[str] = [
        f'{prefix}DECOMPOSITION ({len(created)} children):',
        '',
        decomposition.rationale.strip() or '(no rationale)',
        '',
    ]
    if archived_ids:
        rationale_lines.extend(
            [
                f'Archived {len(archived_ids)} previous child(ren): '
                + ', '.join(archived_ids),
                '',
            ]
        )
    rationale_lines.append('Children:')
    for child, spec in zip(created, decomposition.children, strict=False):
        rationale_lines.append(
            f'- {child.identifier} ({spec.estimate}pt): {spec.title}'
        )
    linear.add_comment(issue.id, '\n'.join(rationale_lines))

    new_parent_labels = _add_label(issue.label_ids, label_lookup, 'kind:decomposition')
    target = states.get(role.target_state_on_success)
    if target is None:
        return None, False
    linear.update_issue(
        issue.id,
        IssueUpdate(
            state_id=target,
            unset_assignee=True,
            label_ids=new_parent_labels,
        ),
    )
    log.info(
        '%s decomposed into %d children -> %s',
        issue.identifier,
        len(created),
        role.target_state_on_success,
    )
    return None, True


def _format_child_description(spec: ChildSpec, *, parent_identifier: str) -> str:
    bullets = '\n'.join(f'* {ac}' for ac in spec.acceptance_criteria)
    return (
        f'{spec.description}\n\n'
        f'## Acceptance Criteria\n\n'
        f'{bullets}\n\n'
        f'## Notes\n\nParent: {parent_identifier}.\n'
    )


def _resolve_team_id_for_issue(linear: LinearClient, issue: Issue) -> str:
    document = """
        query TeamForIssue($id: String!) {
          issue(id: $id) {
            team { id }
          }
        }
    """
    data = linear.query(document, {'id': issue.identifier})
    return str(data['issue']['team']['id'])


def _resolve_project_id_for_issue(linear: LinearClient, issue: Issue) -> str | None:
    """Look up the parent issue's Linear project to inherit it onto children.

    Architect children must land in the same project as their parent so
    multi-project polling stays scoped. Returns None when the parent has
    no project association (legacy issues), in which case children are
    created without a project too.
    """
    document = """
        query ProjectForIssue($id: String!) {
          issue(id: $id) {
            project { id }
          }
        }
    """
    data = linear.query(document, {'id': issue.identifier})
    issue_node = cast('dict[str, Any]', data.get('issue') or {})
    project = cast('dict[str, Any] | None', issue_node.get('project'))
    if not project:
        return None
    pid = project.get('id')
    return str(pid) if pid is not None else None


def _resolve_team_labels(linear: LinearClient, issue: Issue) -> dict[str, str]:
    """Map label name → id for the issue's team. Cheap one-shot per spawn."""
    document = """
        query LabelsForIssue($id: String!) {
          issue(id: $id) {
            team {
              labels(first: 250) { nodes { id name } }
            }
          }
        }
    """
    data = linear.query(document, {'id': issue.identifier})
    nodes = data['issue']['team']['labels']['nodes']
    return {n['name']: n['id'] for n in nodes}


def _add_label(
    current_ids: tuple[str, ...],
    label_lookup: dict[str, str],
    label_name: str,
) -> tuple[str, ...]:
    target_id = label_lookup.get(label_name)
    if target_id is None:
        log.warning(
            'label %r is not provisioned on this team — skipping add. '
            'Run `albedo setup` to provision required labels.',
            label_name,
        )
        return current_ids
    if target_id in current_ids:
        return current_ids
    return (*current_ids, target_id)


def _remove_label(
    current_ids: tuple[str, ...],
    label_lookup: dict[str, str],
    label_name: str,
) -> tuple[str, ...]:
    target_id = label_lookup.get(label_name)
    if target_id is None:
        return current_ids
    return tuple(lid for lid in current_ids if lid != target_id)


def _set_attempts_label(
    current_ids: tuple[str, ...],
    label_lookup: dict[str, str],
    n: int,
) -> tuple[str, ...]:
    """Replace any existing `attempts:*` label with `attempts:N`."""
    attempts_ids = {
        lid for name, lid in label_lookup.items() if name.startswith('attempts:')
    }
    filtered = tuple(lid for lid in current_ids if lid not in attempts_ids)
    target = label_lookup.get(f'attempts:{n}')
    if target is None:
        return filtered
    return (*filtered, target)
