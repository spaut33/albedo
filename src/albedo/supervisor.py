"""Multi-worker supervisor.

The default `albedo` invocation spawns
`config.workers` independent worker processes. Each child runs the same
`run_loop` against Linear, but with its own `agent_id` (string label "1",
"2", ...) and its own per-agent Linear token (`LINEAR_API_KEY_<id>`,
falling back to the shared `LINEAR_API_KEY`).

The parent process is the supervisor: spawn → wait → forward SIGTERM/SIGINT
to children → join. Children get their own SIGTERM handler from
`run_loop` itself, so they finish the current spawn before exiting.
"""

from __future__ import annotations

import contextlib
import logging
import multiprocessing as mp
import signal
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from multiprocessing.process import BaseProcess
from pathlib import Path
from types import FrameType

from albedo.ci_redispatch import redispatch_on_failed_ci
from albedo.comment_redispatch import TriageFn, redispatch_on_new_user_comments
from albedo.comment_triage import triage_user_reply
from albedo.config import OrchestratorConfig, load_github_pat, load_linear_api_key
from albedo.dispatch_messages import DispatchQueue, ResultQueue
from albedo.github_client import GithubClient
from albedo.housekeeping import (
    archive_old_done_issues,
    complete_decompositions_when_children_done,
    gc_worktrees,
    recover_stale_claims,
    release_decomposed_children,
    sync_merged_prs_to_done,
)
from albedo.linear_client import LinearClient
from albedo.logging_setup import configure_logging
from albedo.poller import Poller, run_poller, run_result_drain
from albedo.usage import UsageLedger, default_db_path
from albedo.worker import run_loop

DEFAULT_HOUSEKEEPING_INTERVAL_SECONDS = 30
DEFAULT_ARCHIVE_INTERVAL_SECONDS = 24 * 60 * 60

log = logging.getLogger(__name__)

type WorkerEntry = Callable[
    [
        str,
        OrchestratorConfig,
        Path,
        Path | None,
        frozenset[str],
        bool,
        DispatchQueue,
        ResultQueue,
    ],
    None,
]


@dataclass(frozen=True, slots=True)
class SupervisorOptions:
    workers: int
    config: OrchestratorConfig
    prompts_dir: Path
    mcp_config_path: Path | None
    skip_stale_recovery: bool
    enable_tui: bool = False
    project_label: str = ''


def supervise(
    options: SupervisorOptions,
    *,
    worker_entry: WorkerEntry | None = None,
    spawn_method: str | None = 'spawn',
) -> None:
    """Spawn N worker processes and supervise them until they all exit.

    `worker_entry` is the callable each child runs; injected for tests.
    Production callers leave it None and we use the in-module default.
    `spawn_method='spawn'` — fork/forkserver are unsafe under SQLite/MCP.
    Pass `spawn_method=None` only in tests where the parent doesn't actually
    need to fork (the `worker_entry` is then run inline-equivalent via mocks).
    """
    entry = worker_entry or _default_worker_entry
    if spawn_method is not None:
        mp.set_start_method(spawn_method, force=True)
    children: list[BaseProcess] = []

    if not options.skip_stale_recovery:
        _run_stale_recovery_once(options)

    bot_user_ids = _discover_bot_user_ids(options)

    queue_size = options.workers * options.config.dispatch.queue_size_per_worker
    dispatch_queue: DispatchQueue = mp.Queue(maxsize=queue_size)
    result_queue: ResultQueue = mp.Queue()
    in_flight: dict[str, float] = {}
    in_flight_lock = threading.Lock()

    housekeeping_stop = threading.Event()
    housekeeping_thread = threading.Thread(
        target=_housekeeping_loop,
        args=(options, housekeeping_stop, bot_user_ids),
        name='housekeeping',
        daemon=True,
    )
    housekeeping_thread.start()

    poller_stop = threading.Event()
    poller_thread = threading.Thread(
        target=_poller_loop,
        args=(options, dispatch_queue, in_flight, in_flight_lock, poller_stop),
        name='poller',
        daemon=True,
    )
    poller_thread.start()

    drain_stop = threading.Event()
    drain_thread = threading.Thread(
        target=run_result_drain,
        kwargs={
            'result_queue': result_queue,
            'in_flight': in_flight,
            'in_flight_lock': in_flight_lock,
            'stop': drain_stop,
        },
        name='result-drain',
        daemon=True,
    )
    drain_thread.start()

    for n in range(1, options.workers + 1):
        agent_id = str(n)
        process: BaseProcess = mp.Process(
            target=entry,
            args=(
                agent_id,
                options.config,
                options.prompts_dir,
                options.mcp_config_path,
                bot_user_ids,
                options.enable_tui,
                dispatch_queue,
                result_queue,
            ),
            name=f'agent-{agent_id}',
            daemon=False,
        )
        process.start()
        log.info('spawned worker %s (pid=%s)', process.name, process.pid)
        children.append(process)

    _install_forwarding_handlers(children, housekeeping_stop)

    if options.enable_tui:
        _run_with_tui(options, children, housekeeping_stop)
    else:
        _wait_for_children(children)

    poller_stop.set()
    for _ in range(options.workers):
        with contextlib.suppress(Exception):
            dispatch_queue.put_nowait(None)
    drain_stop.set()
    with contextlib.suppress(Exception):
        result_queue.put_nowait(None)
    housekeeping_stop.set()
    poller_thread.join(timeout=5)
    drain_thread.join(timeout=5)
    housekeeping_thread.join(timeout=5)


def _make_terminate_worker(
    children: Sequence[BaseProcess],
) -> Callable[[str], bool]:
    """Build the per-worker terminate callable handed to the TUI.

    Scans `children` for a process whose `.name` matches `agent-<id>` and
    sends SIGTERM via `.terminate()`. Returns True when a live match was
    signalled, False otherwise (no match, already exited, or terminate
    raised). Logs every outcome at INFO/WARNING so operators can see the
    Ctrl+K request even when the TUI is in alt-screen mode.
    """

    def terminate_worker(agent_id: str) -> bool:
        target_name = f'agent-{agent_id}'
        for child in children:
            if child.name != target_name:
                continue
            if not child.is_alive():
                log.info('terminate_worker: %s already exited', target_name)
                return False
            try:
                child.terminate()
            except Exception as exc:
                log.warning(
                    'terminate_worker: failed to terminate %s: %s', target_name, exc
                )
                return False
            log.info('terminate_worker: sent SIGTERM to %s', target_name)
            return True
        log.info('terminate_worker: no child named %s', target_name)
        return False

    return terminate_worker


def _run_with_tui(
    options: SupervisorOptions,
    children: list[BaseProcess],
    housekeeping_stop: threading.Event,
) -> None:
    """Drive the live TUI on the main thread until exit, then join children."""
    from albedo.stats import (
        StatsContext,
        daily_cost_usd,
        daily_summary,
        session_cost_usd,
        session_summary,
    )
    from albedo.tui.app import TuiApp, TuiContext
    from albedo.usage import UsageLedger, default_db_path

    ledger = UsageLedger(default_db_path(options.config.state_dir))
    stats_ctx = StatsContext(
        ledger=ledger,
        session_started_at_unix=int(time.time()),
        cache_seconds=2.0,
    )

    def warnings_provider() -> str:
        msgs: list[str] = []
        from albedo.heartbeat import heartbeat_path, is_stale
        from albedo.usage_limit import format_pause_summary, is_paused

        state_dir = options.config.state_dir
        pause_state = is_paused(state_dir)
        if pause_state is not None:
            msgs.append(format_pause_summary(pause_state))
        for child in children:
            agent_id = child.name.removeprefix('agent-')
            stale = is_stale(heartbeat_path(state_dir, agent_id))
            if stale and child.is_alive():
                msgs.append(f'agent-{agent_id} heartbeat stale')
        if msgs:
            return '⚠ ' + ' · '.join(msgs)
        return ''

    def all_exited() -> bool:
        return all(not c.is_alive() for c in children)

    def shutdown_in_progress() -> bool:
        # housekeeping_stop is set the moment SIGINT/SIGTERM is forwarded;
        # workers may take a second or two to exit afterwards.
        return housekeeping_stop.is_set() and not all_exited()

    terminate_worker = _make_terminate_worker(children)

    ctx = TuiContext(
        state_dir=options.config.state_dir,
        project_name=options.project_label or 'albedo',
        expected_workers=options.workers,
        log_dir=options.config.state_dir / 'logs',
        session_summary_provider=lambda: session_summary(stats_ctx),
        daily_summary_provider=lambda: daily_summary(stats_ctx),
        session_cost_provider=lambda: session_cost_usd(stats_ctx),
        daily_cost_provider=lambda: daily_cost_usd(stats_ctx),
        warnings_provider=warnings_provider,
        shutdown_in_progress=shutdown_in_progress,
        terminate_worker=terminate_worker,
        should_stop=all_exited,
        token_cap=options.config.usage.rolling_window_token_cap,
        ledger=ledger,
    )

    try:
        TuiApp(ctx).run()
    finally:
        housekeeping_stop.set()
        for child in children:
            if child.is_alive():
                try:
                    child.terminate()
                except Exception as exc:
                    log.warning('failed to terminate %s: %s', child.name, exc)
        _wait_for_children(children)


def _run_stale_recovery_once(options: SupervisorOptions) -> None:
    """Recover stale claims and GC stale worktrees once at startup.

    Uses the shared Linear token (`LINEAR_API_KEY` / explicit env), since
    the agent users themselves may not have visibility into each other's
    issues. Workers that are about to start are listed as `active_agent_ids`
    so we don't strip claims that belong to them. Worktree GC catches
    leftovers from prior sessions where the supervisor exited before its
    30s housekeeping tick removed them.
    """
    api_key = load_linear_api_key()
    active_ids = [str(n) for n in range(1, options.workers + 1)]
    with LinearClient(options.config.linear.api_url, api_key) as client:
        viewer = client.viewer()
        report = recover_stale_claims(
            linear=client,
            state_dir=options.config.state_dir,
            agent_user_id=viewer.id,
            active_agent_ids=active_ids,
            project_id=options.config.linear.project_id,
        )
        try:
            gc_report = gc_worktrees(linear=client, repo_path=options.config.repo.path)
        except Exception as exc:
            log.warning('startup worktree GC failed: %s', exc)
            gc_report = None
    if report.released:
        log.info(
            'stale-claim recovery: released %d issue(s): %s',
            len(report.released),
            ', '.join(i.identifier for i in report.released),
        )
    else:
        log.info('stale-claim recovery: %d assigned, none stale', report.inspected)
    if gc_report is not None and gc_report.removed:
        log.info(
            'startup worktree GC: removed %d worktree(s) (%d inspected)',
            len(gc_report.removed),
            gc_report.inspected,
        )


def _discover_bot_user_ids(options: SupervisorOptions) -> frozenset[str]:
    """Collect Linear user ids of per-agent tokens (`LINEAR_API_KEY_<id>`).

    Used by workers and housekeeping to filter the bots' own comments out
    of the user-feedback prompt block. The shared `LINEAR_API_KEY` is
    deliberately NOT included: typical deployments reuse the operator's
    personal Linear token there, and adding their id would silently
    filter every comment that operator writes. Housekeeping comments
    posted under the shared key are filtered instead via their
    `**housekeeping**:` prefix in `comment_filter.AGENT_PREFIX_RE`.

    Missing tokens are skipped with a warning so a misconfigured agent
    slot doesn't take the whole supervisor down at startup.
    """
    ids: set[str] = set()
    for n in range(1, options.workers + 1):
        agent_id = str(n)
        try:
            api_key = load_linear_api_key(agent_id=agent_id)
        except RuntimeError as exc:
            log.warning('bot-id discovery: agent-%s token missing: %s', agent_id, exc)
            continue
        try:
            with LinearClient(options.config.linear.api_url, api_key) as client:
                ids.add(client.viewer().id)
        except Exception as exc:
            log.warning('bot-id discovery: agent-%s viewer() failed: %s', agent_id, exc)
    log.info('bot-user-id set: %d distinct agent id(s)', len(ids))
    return frozenset(ids)


def _install_forwarding_handlers(
    children: list[BaseProcess],
    housekeeping_stop: threading.Event,
) -> None:
    invocations = 0

    def forward(signum: int, _frame: FrameType | None) -> None:
        nonlocal invocations
        invocations += 1
        if invocations == 1:
            log.info(
                'supervisor received signal %s; forwarding SIGTERM to '
                '%d worker(s) (press again to force-quit)',
                signum,
                len(children),
            )
            housekeeping_stop.set()
            for child in children:
                if child.is_alive():
                    try:
                        child.terminate()
                    except Exception as exc:
                        log.warning('failed to signal %s: %s', child.name, exc)
            return

        log.warning(
            'supervisor received signal %s again; escalating to SIGKILL on '
            'still-alive worker(s)',
            signum,
        )
        for child in children:
            if child.is_alive():
                try:
                    child.kill()
                except Exception as exc:
                    log.warning('failed to kill %s: %s', child.name, exc)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)


def _housekeeping_loop(
    options: SupervisorOptions,
    stop: threading.Event,
    bot_user_ids: frozenset[str] = frozenset(),
) -> None:
    """Periodic housekeeping tick.

    Runs in the supervisor process (separate thread, daemon). Uses the
    shared Linear token — the housekeeping work is independent of which
    agent user discovered the work. Each tick:
      1. Releases children of approved decompositions (Triage → Backlog).
      2. Moves Linear issues whose PR has been merged on GitHub to Done.
    """
    interval = DEFAULT_HOUSEKEEPING_INTERVAL_SECONDS
    api_key = load_linear_api_key()
    github_pat = load_github_pat()
    next_archive_at = 0.0
    with LinearClient(options.config.linear.api_url, api_key) as client:
        team = client.resolve_team(options.config.linear.team)
        github = GithubClient(github_pat) if github_pat is not None else None
        try:
            while not stop.is_set():
                try:
                    report = release_decomposed_children(
                        linear=client,
                        team_id=team.id,
                        project_id=options.config.linear.project_id,
                    )
                    if report.children_released:
                        log.info(
                            'housekeeping: released %d child(ren) from '
                            'approved decompositions',
                            len(report.children_released),
                        )
                except Exception as exc:
                    log.warning('housekeeping decomposition tick failed: %s', exc)

                try:
                    rollup_report = complete_decompositions_when_children_done(
                        linear=client,
                        team_id=team.id,
                        project_id=options.config.linear.project_id,
                    )
                    if rollup_report.parents_completed:
                        log.info(
                            'housekeeping: closed %d decomposition parent(s) '
                            'with all children finished',
                            len(rollup_report.parents_completed),
                        )
                except Exception as exc:
                    log.warning(
                        'housekeeping decomposition rollup tick failed: %s', exc
                    )

                if github is not None:
                    try:
                        merged_report = sync_merged_prs_to_done(
                            linear=client,
                            github=github,
                            team_id=team.id,
                            project_id=options.config.linear.project_id,
                        )
                        if merged_report.moved_to_done:
                            log.info(
                                'housekeeping: moved %d issue(s) to Done '
                                'after PR merge',
                                len(merged_report.moved_to_done),
                            )
                    except Exception as exc:
                        log.warning('housekeeping merge-sync tick failed: %s', exc)

                try:
                    gc_report = gc_worktrees(
                        linear=client, repo_path=options.config.repo.path
                    )
                    if gc_report.removed:
                        log.info(
                            'housekeeping: removed %d worktree(s)',
                            len(gc_report.removed),
                        )
                except Exception as exc:
                    log.warning('housekeeping worktree GC tick failed: %s', exc)

                if options.config.features.comment_redispatch:
                    triage_fn = (
                        _build_triage_fn(options)
                        if options.config.features.comment_triage
                        else None
                    )
                    try:
                        redispatch_report = redispatch_on_new_user_comments(
                            linear=client,
                            team_id=team.id,
                            bot_user_ids=bot_user_ids,
                            state_dir=options.config.state_dir,
                            project_id=options.config.linear.project_id,
                            triage_fn=triage_fn,
                        )
                        if redispatch_report.fired:
                            log.info(
                                'housekeeping: redispatched %d issue(s) on '
                                'new user comments',
                                len(redispatch_report.fired),
                            )
                        if redispatch_report.skipped_non_substantive:
                            log.info(
                                'housekeeping: triage skipped %d non-substantive '
                                'reply/replies (%s)',
                                len(redispatch_report.skipped_non_substantive),
                                ', '.join(redispatch_report.skipped_non_substantive),
                            )
                    except Exception as exc:
                        log.warning('housekeeping redispatch tick failed: %s', exc)

                if (
                    options.config.features.ci_failed_pr_dispatch
                    and github is not None
                    and options.config.repo.github is not None
                ):
                    try:
                        ci_report = redispatch_on_failed_ci(
                            linear=client,
                            github=github,
                            state_dir=options.config.state_dir,
                            repo_owner=options.config.repo.github.owner,
                            repo_name=options.config.repo.github.repo,
                            team_id=team.id,
                            project_id=options.config.linear.project_id,
                        )
                        if ci_report.fired:
                            log.info(
                                'housekeeping: redispatched %d issue(s) on failed CI',
                                len(ci_report.fired),
                            )
                        if ci_report.seeded:
                            log.info(
                                'housekeeping: seeded %d CI run(s) for future '
                                'failure detection (%s)',
                                len(ci_report.seeded),
                                ', '.join(ci_report.seeded),
                            )
                    except Exception as exc:
                        log.warning('housekeeping CI redispatch tick failed: %s', exc)

                now_mono = time.monotonic()
                if (
                    options.config.archive_done_after_days > 0
                    and now_mono >= next_archive_at
                ):
                    try:
                        archive_report = archive_old_done_issues(
                            linear=client,
                            team_id=team.id,
                            older_than_days=options.config.archive_done_after_days,
                            project_id=options.config.linear.project_id,
                        )
                        if archive_report.archived:
                            log.info(
                                'housekeeping: archived %d Done issue(s) '
                                'older than %d day(s)',
                                len(archive_report.archived),
                                options.config.archive_done_after_days,
                            )
                    except Exception as exc:
                        log.warning('housekeeping archive tick failed: %s', exc)
                    next_archive_at = now_mono + DEFAULT_ARCHIVE_INTERVAL_SECONDS

                stop.wait(interval)
        finally:
            if github is not None:
                github.close()


def _build_triage_fn(options: SupervisorOptions) -> TriageFn:
    """Closure that adapts `triage_user_reply` to the redispatch callback shape.

    Runs `claude -p` in the orchestrator's state dir — no tools or MCP are
    enabled for the call, so the cwd choice only affects where transient
    files (none, in practice) might land.
    """
    cwd = options.config.state_dir
    model = options.config.models.triage

    def fn(agent_message: str, user_replies: Sequence[str]) -> bool:
        verdict = triage_user_reply(
            agent_message=agent_message,
            user_replies=user_replies,
            cwd=cwd,
            model=model,
        )
        log.info(
            'triage verdict: substantive=%s reason=%s',
            verdict.is_substantive,
            verdict.reason,
        )
        return verdict.is_substantive

    return fn


def _wait_for_children(children: list[BaseProcess]) -> None:
    for child in children:
        child.join()
        log.info('worker %s exited with code %s', child.name, child.exitcode)


def _poller_loop(
    options: SupervisorOptions,
    dispatch_queue: DispatchQueue,
    in_flight: dict[str, float],
    in_flight_lock: threading.Lock,
    stop: threading.Event,
) -> None:
    """Run the single-poller fan-out for the lifetime of the supervisor.

    Uses the shared `LINEAR_API_KEY` (same identity as housekeeping) —
    workers still claim with their per-agent token, so the supervisor
    never needs an agent identity for discovery.
    """
    api_key = load_linear_api_key()
    ledger = UsageLedger(default_db_path(options.config.state_dir))
    with LinearClient(options.config.linear.api_url, api_key) as client:
        team = client.resolve_team(options.config.linear.team)
        poller = Poller(
            linear=client,
            config=options.config,
            team_id=team.id,
            dispatch_queue=dispatch_queue,
            in_flight=in_flight,
            in_flight_lock=in_flight_lock,
            ledger=ledger,
        )
        run_poller(
            poller=poller,
            poll_interval_seconds=options.config.poll_interval_seconds,
            stop=stop,
        )


def _default_worker_entry(
    agent_id: str,
    config: OrchestratorConfig,
    prompts_dir: Path,
    mcp_config_path: Path | None,
    bot_user_ids: frozenset[str],
    silence_stderr: bool,
    dispatch_queue: DispatchQueue,
    result_queue: ResultQueue,
) -> None:
    """Child-process entrypoint.

    Re-resolves secrets per-child so each worker's Linear token can be
    different (per `LINEAR_API_KEY_<id>`). Logging is reconfigured because
    `multiprocessing` with `spawn` does not inherit handlers — each worker
    writes structured JSON to its own `state/logs/agent-<id>.log`.

    `silence_stderr` is passed by the supervisor when the live TUI is
    active: workers must not write log lines to stderr because that
    breaks the Rich Live alt-screen rendering.
    """
    configure_logging(
        agent_id=agent_id,
        level='INFO',
        state_dir=config.state_dir,
        silence_stderr=silence_stderr,
    )
    api_key = load_linear_api_key(agent_id=agent_id)
    github_pat = load_github_pat(agent_id=agent_id)
    with LinearClient(config.linear.api_url, api_key) as client:
        viewer = client.viewer()
        log.info(
            'worker agent-%s authenticated as %s (%s)',
            agent_id,
            viewer.name,
            viewer.email,
        )
        run_loop(
            config=config,
            linear=client,
            agent_id=agent_id,
            agent_user_id=viewer.id,
            prompts_dir=prompts_dir,
            mcp_config_path=mcp_config_path,
            dispatch_queue=dispatch_queue,
            result_queue=result_queue,
            github_pat=github_pat,
            bot_user_ids=bot_user_ids,
            display_name=viewer.name,
        )
