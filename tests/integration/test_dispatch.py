"""End-to-end test of the supervisor → workers dispatch path.

Two thread-workers race against a real `mp.Queue` populated by the
poller. Each candidate must be consumed by exactly one worker; no two
workers may try-claim the same id; every issue must produce a TaskDone.
With the old per-worker poll path this would require real Linear and
still be flaky — with the queue, exactly-once delivery is structural
(`mp.Queue.get` is atomic) and the test is deterministic.
"""

from __future__ import annotations

import multiprocessing as mp
import threading
import time
from collections import Counter
from pathlib import Path
from typing import cast

from albedo.config import (
    DispatchConfig,
    LinearConfig,
    OrchestratorConfig,
    RepoConfig,
    UsageConfig,
)
from albedo.dispatch_messages import (
    ClaimedOk,
    DispatchQueue,
    ResultQueue,
    TaskDone,
)
from albedo.linear_client import Issue, LinearClient
from albedo.poller import Poller, run_result_drain
from albedo.usage import UsageLedger


def _issue(issue_id: str) -> Issue:
    return Issue(
        id=issue_id,
        identifier=issue_id,
        title=issue_id,
        description='',
        state_id='s',
        state_name='Backlog',
        assignee_id=None,
        label_ids=(),
        label_names=(),
        parent_id=None,
        branch_name='',
    )


class _FakeLinear:
    def __init__(self, pickup: list[Issue]) -> None:
        self._pickup = pickup
        self.pickup_calls = 0

    def list_pickup_issues(
        self,
        _team_id: str,
        _states: list[str],
        *,
        exclude_labels: list[str] | None = None,
        project_id: str | None = None,
    ) -> list[Issue]:
        del exclude_labels, project_id
        self.pickup_calls += 1
        return list(self._pickup)

    def list_active_issues(
        self,
        _team_id: str,
        *,
        project_id: str | None = None,
    ) -> list[Issue]:
        del project_id
        return list(self._pickup)


class _FakeLedger:
    def should_throttle(self, *, token_cap: int, window_seconds: int) -> bool:
        del token_cap, window_seconds
        return False


def _config(tmp_path: Path) -> OrchestratorConfig:
    return OrchestratorConfig(
        workers=2,
        project_name='sample',
        repo=RepoConfig(path=tmp_path / 'repo', base_branch='main'),
        linear=LinearConfig(team='ORC'),
        worktree_root=tmp_path / 'wt',
        state_dir=tmp_path / 'state',
        dispatch=DispatchConfig(queue_size_per_worker=10),
        usage=UsageConfig(),
    )


def _consumer(
    *,
    name: str,
    dispatch_queue: DispatchQueue,
    result_queue: ResultQueue,
    claimed_log: list[tuple[str, str]],
    log_lock: threading.Lock,
    stop: threading.Event,
) -> None:
    """Stripped-down worker loop: consume → 'claim' → done.

    No Linear, no spawn — just enough to verify queue exclusivity and
    that the worker emits ClaimedOk + TaskDone for each issue it owns.
    """
    while not stop.is_set():
        try:
            msg = dispatch_queue.get(timeout=0.2)
        except Exception:
            continue
        if msg is None:
            break
        issue_id = msg.issue.id
        with log_lock:
            claimed_log.append((name, issue_id))
        result_queue.put_nowait(ClaimedOk(issue_id=issue_id))
        # Simulate a tiny bit of work so the race surface is real.
        time.sleep(0.01)
        result_queue.put_nowait(TaskDone(issue_id=issue_id))


def test_two_workers_consume_each_candidate_exactly_once(tmp_path: Path) -> None:
    issues = [_issue(f'i{n}') for n in range(10)]
    fake = _FakeLinear(pickup=issues)
    cfg = _config(tmp_path)

    dispatch_queue: DispatchQueue = mp.Queue(
        maxsize=cfg.workers * cfg.dispatch.queue_size_per_worker
    )
    result_queue: ResultQueue = mp.Queue()
    in_flight: dict[str, float] = {}
    in_flight_lock = threading.Lock()

    poller = Poller(
        linear=cast(LinearClient, fake),
        config=cfg,
        team_id='team-1',
        dispatch_queue=dispatch_queue,
        in_flight=in_flight,
        in_flight_lock=in_flight_lock,
        ledger=cast(UsageLedger, _FakeLedger()),
    )

    drain_stop = threading.Event()
    drain_thread = threading.Thread(
        target=run_result_drain,
        kwargs={
            'result_queue': result_queue,
            'in_flight': in_flight,
            'in_flight_lock': in_flight_lock,
            'stop': drain_stop,
            'poll_timeout_seconds': 0.1,
        },
        daemon=True,
    )
    drain_thread.start()

    claimed_log: list[tuple[str, str]] = []
    log_lock = threading.Lock()
    worker_stop = threading.Event()
    workers = [
        threading.Thread(
            target=_consumer,
            kwargs={
                'name': f'w{n}',
                'dispatch_queue': dispatch_queue,
                'result_queue': result_queue,
                'claimed_log': claimed_log,
                'log_lock': log_lock,
                'stop': worker_stop,
            },
            daemon=True,
        )
        for n in range(cfg.workers)
    ]
    for w in workers:
        w.start()

    poller.tick()

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        with log_lock:
            done = len(claimed_log) >= len(issues)
        if done:
            break
        time.sleep(0.05)

    worker_stop.set()
    for _ in workers:
        dispatch_queue.put_nowait(None)
    for w in workers:
        w.join(timeout=2)

    drain_deadline = time.monotonic() + 5.0
    while time.monotonic() < drain_deadline:
        with in_flight_lock:
            empty = not in_flight
        if empty:
            break
        time.sleep(0.05)

    drain_stop.set()
    result_queue.put_nowait(None)
    drain_thread.join(timeout=2)

    consumed = [issue_id for _name, issue_id in claimed_log]
    counts = Counter(consumed)
    assert set(consumed) == {i.id for i in issues}, (
        f'missing or extra: {sorted(set(consumed))}'
    )
    assert all(c == 1 for c in counts.values()), (
        f'duplicate claims: {[k for k, v in counts.items() if v > 1]}'
    )
    in_flight_after = dict(in_flight)
    assert in_flight_after == {}, f'leftover in_flight entries: {in_flight_after}'
    assert fake.pickup_calls == 1
