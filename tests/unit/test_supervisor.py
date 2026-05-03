"""Tests for the multi-worker supervisor.

We don't actually fork — `multiprocessing.Process` is replaced with a
double that synchronously records calls. This keeps tests fast and
deterministic; real fork/spawn behaviour is exercised by the E2E run.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest

from albedo import supervisor as sup
from albedo.config import (
    LinearConfig,
    OrchestratorConfig,
    RepoConfig,
)
from albedo.supervisor import SupervisorOptions, supervise


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

    def start(self) -> None:
        self._alive = True
        type(self).started.append(self)

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.terminate_calls += 1
        self._alive = False
        self.exitcode = -15

    def join(self, timeout: float | None = None) -> None:
        del timeout
        self._alive = False
        if self.exitcode is None:
            self.exitcode = 0
        type(self).joined.append(self)


def _dummy_entry(*_a: object) -> None:
    return None


@pytest.fixture(autouse=True)
def _reset_fake_process() -> None:  # pyright: ignore[reportUnusedFunction]
    _FakeProcess.started = []
    _FakeProcess.joined = []


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
