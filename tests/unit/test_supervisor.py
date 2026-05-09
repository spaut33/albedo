"""Tests for the multi-worker supervisor.

We don't actually fork — `multiprocessing.Process` is replaced with a
double that synchronously records calls. This keeps tests fast and
deterministic; real fork/spawn behaviour is exercised by the E2E run.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar

import pytest

from albedo import supervisor as sup
from albedo.ci_redispatch import CiRedispatchReport
from albedo.config import (
    FeaturesConfig,
    GithubConfig,
    LinearConfig,
    OrchestratorConfig,
    RepoConfig,
)
from albedo.linear_client import Team
from albedo.supervisor import (
    SupervisorOptions,
    _housekeeping_loop,  # pyright: ignore[reportPrivateUsage]
    supervise,
)


class _FakeProcess:
    """Drop-in for `multiprocessing.Process` that records inputs."""

    started: ClassVar[list[_FakeProcess]] = []
    joined: ClassVar[list[_FakeProcess]] = []
    next_pid: ClassVar[int] = 1000

    def __init__(
        self,
        *,
        target: Callable[..., None],
        args: tuple[Any, ...],
        name: str,
        daemon: bool,
    ) -> None:
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.pid = type(self).next_pid
        type(self).next_pid += 1
        self.exitcode: int | None = None
        self._alive = False
        self.terminate_calls = 0
        self.kill_calls = 0

    def start(self) -> None:
        self._alive = True
        type(self).started.append(self)

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.terminate_calls += 1
        self._alive = False
        self.exitcode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self._alive = False
        self.exitcode = -9

    def join(self, timeout: float | None = None) -> None:
        del timeout
        self._alive = False
        if self.exitcode is None:
            self.exitcode = 0
        type(self).joined.append(self)


def _dummy_entry(*_a: object) -> None:
    return None


@pytest.fixture(autouse=True)
def _reset_fake_process(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeProcess.started = []
    _FakeProcess.joined = []

    # The poller and housekeeping loops would otherwise spin up a real
    # LinearClient against a non-existent team and raise from a daemon
    # thread, surfacing as PytestUnhandledThreadExceptionWarning. Replace
    # them with a minimal no-op that just blocks until the stop event fires
    # so `thread.join()` returns promptly.
    def _block_on_stop(*args: object, **_kw: object) -> None:
        for arg in args:
            if isinstance(arg, threading.Event):
                arg.wait()
                return

    monkeypatch.setattr(sup, '_housekeeping_loop', _block_on_stop)
    monkeypatch.setattr(sup, '_poller_loop', _block_on_stop)


def _options(workers: int = 2, *, skip: bool = True) -> SupervisorOptions:
    cfg = OrchestratorConfig(
        workers=workers,
        project_name='sample',
        repo=RepoConfig(path=Path('/tmp/sample'), base_branch='main'),
        linear=LinearConfig(team='ORC'),
        state_dir=Path('/tmp/sample-state'),
        worktree_root=Path('/tmp/sample-wt'),
    )
    return SupervisorOptions(
        workers=workers,
        config=cfg,
        prompts_dir=Path('prompts'),
        mcp_config_path=None,
        skip_stale_recovery=skip,
    )


def test_supervise_spawns_n_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    def _noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(sup.mp, 'Process', _FakeProcess)
    monkeypatch.setattr(sup.mp, 'set_start_method', _noop)
    monkeypatch.setattr(sup.signal, 'signal', _noop)

    entry_calls: list[tuple[Any, ...]] = []

    def fake_entry(*args: Any) -> None:
        entry_calls.append(args)

    supervise(
        _options(workers=3, skip=True),
        worker_entry=fake_entry,
        spawn_method=None,
    )

    assert len(_FakeProcess.started) == 3
    assert len(_FakeProcess.joined) == 3
    names = [p.name for p in _FakeProcess.started]
    assert names == ['agent-1', 'agent-2', 'agent-3']
    targets = [p.target for p in _FakeProcess.started]
    assert all(t is fake_entry for t in targets)
    agent_ids = [p.args[0] for p in _FakeProcess.started]
    assert agent_ids == ['1', '2', '3']


def test_supervise_runs_recovery_when_not_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(sup.mp, 'Process', _FakeProcess)
    monkeypatch.setattr(sup.mp, 'set_start_method', _noop)
    monkeypatch.setattr(sup.signal, 'signal', _noop)

    recovery_calls: list[object] = []

    def fake_recovery(opts: object) -> None:
        recovery_calls.append(opts)

    monkeypatch.setattr(sup, '_run_stale_recovery_once', fake_recovery)

    supervise(
        _options(workers=2, skip=False),
        worker_entry=_dummy_entry,
        spawn_method=None,
    )

    assert len(recovery_calls) == 1
    assert len(_FakeProcess.started) == 2


def test_supervise_skips_recovery_when_flag_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(sup.mp, 'Process', _FakeProcess)
    monkeypatch.setattr(sup.mp, 'set_start_method', _noop)
    monkeypatch.setattr(sup.signal, 'signal', _noop)

    recovery_calls: list[object] = []

    def _capture_recovery(opts: object) -> None:
        recovery_calls.append(opts)

    monkeypatch.setattr(sup, '_run_stale_recovery_once', _capture_recovery)

    supervise(
        _options(workers=1, skip=True),
        worker_entry=_dummy_entry,
        spawn_method=None,
    )
    assert recovery_calls == []


def test_supervise_installs_signal_handlers_that_terminate_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(sup.mp, 'Process', _FakeProcess)
    monkeypatch.setattr(sup.mp, 'set_start_method', _noop)

    handlers: dict[int, Callable[..., object]] = {}

    def fake_signal(num: int, handler: Callable[..., object]) -> object:
        handlers[num] = handler
        return None

    monkeypatch.setattr(sup.signal, 'signal', fake_signal)

    supervise(
        _options(workers=2, skip=True),
        worker_entry=_dummy_entry,
        spawn_method=None,
    )

    assert sup.signal.SIGTERM in handlers
    assert sup.signal.SIGINT in handlers

    # After workers are joined they're not "alive" anymore; firing the handler
    # should be safe (no-op for dead children).
    handlers[sup.signal.SIGTERM](sup.signal.SIGTERM, None)
    # And firing a second time (escalation path) must also be a no-op for
    # already-exited children — kill() must NOT be called on a dead child.
    handlers[sup.signal.SIGTERM](sup.signal.SIGTERM, None)
    for proc in _FakeProcess.started:
        assert proc.kill_calls == 0


def test_second_signal_escalates_to_sigkill_on_live_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second SIGINT delivery must escalate to child.kill() on still-alive workers."""

    handlers: dict[int, Callable[..., object]] = {}

    def fake_signal(num: int, handler: Callable[..., object]) -> object:
        handlers[num] = handler
        return None

    monkeypatch.setattr(sup.signal, 'signal', fake_signal)

    live_children: list[_FakeProcess] = []
    for i in range(2):
        proc = _FakeProcess(
            target=_dummy_entry,
            args=(),
            name=f'agent-{i + 1}',
            daemon=True,
        )
        proc.start()
        # Decouple liveness from terminate(): a stuck worker keeps reporting
        # itself alive even after SIGTERM, which is exactly the scenario the
        # escalation handles.
        proc._alive = True  # pyright: ignore[reportPrivateUsage]

        def _stay_alive(self: _FakeProcess = proc) -> None:
            self.terminate_calls += 1

        proc.terminate = _stay_alive  # type: ignore[method-assign]
        live_children.append(proc)

    housekeeping_stop = threading.Event()
    sup._install_forwarding_handlers(  # pyright: ignore[reportPrivateUsage]
        list(live_children),  # type: ignore[arg-type]
        housekeeping_stop,
    )

    assert sup.signal.SIGINT in handlers
    sigint_handler = handlers[sup.signal.SIGINT]

    sigint_handler(sup.signal.SIGINT, None)
    assert housekeeping_stop.is_set()
    for child in live_children:
        assert child.terminate_calls == 1
        assert child.kill_calls == 0
        assert child.is_alive()  # the stuck worker is still around

    sigint_handler(sup.signal.SIGINT, None)
    for child in live_children:
        assert child.terminate_calls == 1
        assert child.kill_calls >= 1


class _FakeLinearClient:
    """Minimal LinearClient context manager for supervisor housekeeping tests."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> _FakeLinearClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def resolve_team(self, _identifier: str) -> Team:
        return Team(id='team-1', key='ORC', name='Orchestrator')


class _FakeGithubClient:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def close(self) -> None:
        return None


def _ci_options(
    *,
    ci_failed_pr_dispatch: bool,
    with_github: bool = True,
    tmp_path: Path,
) -> SupervisorOptions:
    cfg = OrchestratorConfig(
        workers=1,
        project_name='sample',
        repo=RepoConfig(
            path=tmp_path / 'repo',
            base_branch='main',
            github=GithubConfig(owner='octocat', repo='sample')
            if with_github
            else None,
        ),
        linear=LinearConfig(team='ORC'),
        state_dir=tmp_path / 'state',
        worktree_root=tmp_path / 'wt',
        features=FeaturesConfig(
            comment_redispatch=False,
            ci_failed_pr_dispatch=ci_failed_pr_dispatch,
        ),
    )
    return SupervisorOptions(
        workers=1,
        config=cfg,
        prompts_dir=Path('prompts'),
        mcp_config_path=None,
        skip_stale_recovery=True,
    )


def _patch_housekeeping_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace housekeeping side-effects with no-ops so the loop can spin."""

    def _no_release(**_kw: object) -> Any:
        return type('R', (), {'children_released': ()})()

    def _no_sync(**_kw: object) -> Any:
        return type('R', (), {'moved_to_done': ()})()

    def _no_gc(**_kw: object) -> Any:
        return type('R', (), {'removed': ()})()

    def _no_archive(**_kw: object) -> Any:
        return type('R', (), {'archived': ()})()

    monkeypatch.setattr(sup, 'DEFAULT_HOUSEKEEPING_INTERVAL_SECONDS', 0.01)
    monkeypatch.setattr(sup, 'load_linear_api_key', lambda: 'fake-linear')
    monkeypatch.setattr(sup, 'load_github_pat', lambda: 'fake-pat')
    monkeypatch.setattr(sup, 'LinearClient', _FakeLinearClient)
    monkeypatch.setattr(sup, 'GithubClient', _FakeGithubClient)
    monkeypatch.setattr(sup, 'release_decomposed_children', _no_release)
    monkeypatch.setattr(sup, 'sync_merged_prs_to_done', _no_sync)
    monkeypatch.setattr(sup, 'gc_worktrees', _no_gc)
    monkeypatch.setattr(sup, 'archive_old_done_issues', _no_archive)


def test_housekeeping_invokes_ci_redispatch_when_flag_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_housekeeping_dependencies(monkeypatch)
    options = _ci_options(ci_failed_pr_dispatch=True, tmp_path=tmp_path)
    stop = threading.Event()

    calls: list[dict[str, Any]] = []

    def fake_ci(**kwargs: Any) -> CiRedispatchReport:
        calls.append(kwargs)
        stop.set()
        return CiRedispatchReport()

    monkeypatch.setattr(sup, 'redispatch_on_failed_ci', fake_ci)

    _housekeeping_loop(options, stop)

    assert len(calls) == 1
    assert calls[0]['team_id'] == 'team-1'
    assert calls[0]['repo_owner'] == 'octocat'
    assert calls[0]['repo_name'] == 'sample'
    assert calls[0]['state_dir'] == options.config.state_dir


def test_housekeeping_skips_ci_redispatch_when_flag_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_housekeeping_dependencies(monkeypatch)
    options = _ci_options(ci_failed_pr_dispatch=False, tmp_path=tmp_path)
    stop = threading.Event()

    calls: list[dict[str, Any]] = []
    tick_count = {'n': 0}

    def stopping_release(**_kw: object) -> Any:
        tick_count['n'] += 1
        if tick_count['n'] >= 1:
            stop.set()
        return type('R', (), {'children_released': ()})()

    monkeypatch.setattr(sup, 'release_decomposed_children', stopping_release)

    def fake_ci(**kwargs: Any) -> CiRedispatchReport:
        calls.append(kwargs)
        return CiRedispatchReport()

    monkeypatch.setattr(sup, 'redispatch_on_failed_ci', fake_ci)

    _housekeeping_loop(options, stop)

    assert calls == []


def test_housekeeping_continues_after_ci_redispatch_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_housekeeping_dependencies(monkeypatch)
    options = _ci_options(ci_failed_pr_dispatch=True, tmp_path=tmp_path)
    stop = threading.Event()

    call_count = {'n': 0}

    def flaky_ci(**_kwargs: Any) -> CiRedispatchReport:
        call_count['n'] += 1
        if call_count['n'] == 1:
            raise RuntimeError('ci tick boom')
        stop.set()
        return CiRedispatchReport()

    monkeypatch.setattr(sup, 'redispatch_on_failed_ci', flaky_ci)

    _housekeeping_loop(options, stop)

    # First tick raised; second tick still fired — loop survived the failure.
    assert call_count['n'] == 2
