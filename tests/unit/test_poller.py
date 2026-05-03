"""Tests for the supervisor's single-poller fan-out."""

from __future__ import annotations

import multiprocessing as mp
import threading
import time
from pathlib import Path
from typing import Any, cast

from albedo.config import (
    DispatchConfig,
    LinearConfig,
    OrchestratorConfig,
    RepoConfig,
    UsageConfig,
)
from albedo.dispatch_messages import (
    CandidateMsg,
    ClaimedOk,
    ClaimLost,
    DispatchQueue,
    ResultQueue,
    TaskDone,
)
from albedo.linear_client import IncomingRelation, Issue, LinearClient
from albedo.poller import (
    SUPERVISOR_AGENT_ID,
    Poller,
    run_result_drain,
)
from albedo.usage import UsageLedger


def _issue(
    issue_id: str,
    *,
    state_name: str = 'Backlog',
    parent_id: str | None = None,
    incoming: tuple[IncomingRelation, ...] = (),
) -> Issue:
    return Issue(
        id=issue_id,
        identifier=issue_id,
        title=issue_id,
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


class _FakeLinear:
    def __init__(
        self,
        *,
        pickup: list[Issue] | None = None,
        active: list[Issue] | None = None,
    ) -> None:
        self._pickup = pickup or []
        self._active = active if active is not None else self._pickup
        self.pickup_calls = 0
        self.active_calls = 0

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
        self.active_calls += 1
        return list(self._active)


class _FakeLedger:
    def __init__(self, *, throttled: bool = False) -> None:
        self._throttled = throttled
        self.calls = 0

    def should_throttle(self, *, token_cap: int, window_seconds: int) -> bool:
        del token_cap, window_seconds
        self.calls += 1
        return self._throttled


def _config(
    tmp_path: Path,
    *,
    queue_size_per_worker: int = 4,
    offer_ttl_seconds: int = 300,
) -> OrchestratorConfig:
    return OrchestratorConfig(
        workers=1,
        project_name='sample',
        repo=RepoConfig(path=tmp_path / 'repo', base_branch='main'),
        linear=LinearConfig(team='ORC'),
        worktree_root=tmp_path / 'wt',
        state_dir=tmp_path / 'state',
        dispatch=DispatchConfig(
            queue_size_per_worker=queue_size_per_worker,
            offer_ttl_seconds=offer_ttl_seconds,
        ),
        usage=UsageConfig(),
    )


def _make_poller(
    *,
    linear: _FakeLinear,
    config: OrchestratorConfig,
    queue_size: int = 4,
    in_flight: dict[str, float] | None = None,
    ledger: _FakeLedger | None = None,
) -> tuple[Poller, DispatchQueue, dict[str, float]]:
    dispatch_queue: DispatchQueue = mp.Queue(maxsize=queue_size)
    in_flight = in_flight if in_flight is not None else {}
    ledger = ledger if ledger is not None else _FakeLedger()
    poller = Poller(
        linear=cast(LinearClient, linear),
        config=config,
        team_id='team-1',
        dispatch_queue=dispatch_queue,
        in_flight=in_flight,
        in_flight_lock=threading.Lock(),
        ledger=cast(UsageLedger, ledger),
    )
    return poller, dispatch_queue, in_flight


def _drain(q: DispatchQueue, *, settle_seconds: float = 0.05) -> list[Any]:
    """Drain an `mp.Queue` after letting its feeder thread settle.

    `mp.Queue.put_nowait` enqueues to a feeder; `empty()` can briefly lie
    immediately after a put. A short sleep before draining is enough to
    let items materialise — long enough for assertions, short enough to
    keep the test fast.
    """
    time.sleep(settle_seconds)
    msgs: list[Any] = []
    while True:
        try:
            msgs.append(q.get(timeout=0.1))
        except Exception:
            break
    return msgs


def test_tick_offers_unseen_candidates_and_marks_in_flight(tmp_path: Path) -> None:
    issues = [_issue('a'), _issue('b')]
    fake = _FakeLinear(pickup=issues)
    poller, q, in_flight = _make_poller(linear=fake, config=_config(tmp_path))

    poller.tick()

    assert fake.pickup_calls == 1
    msgs = _drain(q)
    assert [m.issue.id for m in msgs] == ['a', 'b']
    assert all(isinstance(m, CandidateMsg) for m in msgs)
    assert set(in_flight) == {'a', 'b'}


def test_tick_skips_already_in_flight(tmp_path: Path) -> None:
    issues = [_issue('a'), _issue('b')]
    fake = _FakeLinear(pickup=issues)
    initial = {'a': time.time()}
    poller, q, in_flight = _make_poller(
        linear=fake, config=_config(tmp_path), in_flight=initial
    )

    poller.tick()

    msgs = _drain(q)
    assert [m.issue.id for m in msgs] == ['b']
    assert set(in_flight) == {'a', 'b'}


def test_tick_re_offers_after_offer_ttl(tmp_path: Path) -> None:
    issues = [_issue('a')]
    fake = _FakeLinear(pickup=issues)
    cfg = _config(tmp_path, offer_ttl_seconds=1)
    original_ts = time.time() - 5.0
    poller, q, in_flight = _make_poller(
        linear=fake, config=cfg, in_flight={'a': original_ts}
    )

    poller.tick()

    msgs = _drain(q)
    assert len(msgs) == 1
    assert msgs[0].issue.id == 'a'
    assert 'a' in in_flight
    assert in_flight['a'] > original_ts


def test_tick_filters_triage_children_and_blocked(tmp_path: Path) -> None:
    open_block = IncomingRelation(type='blocks', source_state_type='started')
    fake = _FakeLinear(
        pickup=[
            _issue('triage-child', state_name='Triage', parent_id='p'),
            _issue('blocked', incoming=(open_block,)),
            _issue('ok'),
        ],
    )
    poller, q, in_flight = _make_poller(linear=fake, config=_config(tmp_path))

    poller.tick()

    msgs = _drain(q)
    assert [m.issue.id for m in msgs] == ['ok']
    assert set(in_flight) == {'ok'}


def test_tick_stops_offering_when_queue_full(tmp_path: Path) -> None:
    issues = [_issue(f'i{n}') for n in range(5)]
    fake = _FakeLinear(pickup=issues)
    poller, q, in_flight = _make_poller(
        linear=fake, config=_config(tmp_path), queue_size=2
    )

    poller.tick()

    msgs = _drain(q)
    assert len(msgs) == 2
    assert set(in_flight) == {m.issue.id for m in msgs}


def test_tick_skips_when_throttled(tmp_path: Path) -> None:
    fake = _FakeLinear(pickup=[_issue('a')])
    ledger = _FakeLedger(throttled=True)
    poller, q, in_flight = _make_poller(
        linear=fake, config=_config(tmp_path), ledger=ledger
    )

    poller.tick()

    assert fake.pickup_calls == 0
    assert _drain(q) == []
    assert in_flight == {}


def test_tick_publishes_queue_snapshot_under_supervisor_id(tmp_path: Path) -> None:
    fake = _FakeLinear(pickup=[_issue('a')])
    poller, _q, _in_flight = _make_poller(linear=fake, config=_config(tmp_path))

    poller.tick()

    queue_file = tmp_path / 'state' / 'linear_queue.json'
    assert queue_file.exists()
    import json

    payload = json.loads(queue_file.read_text(encoding='utf-8'))
    assert payload['agent_id'] == SUPERVISOR_AGENT_ID


def test_run_result_drain_removes_id_on_task_done(tmp_path: Path) -> None:
    in_flight: dict[str, float] = {'a': time.time(), 'b': time.time()}
    rq: ResultQueue = mp.Queue()
    rq.put(TaskDone(issue_id='a'))
    rq.put(ClaimLost(issue_id='b'))
    rq.put(None)
    stop = threading.Event()
    run_result_drain(
        result_queue=rq,
        in_flight=in_flight,
        in_flight_lock=threading.Lock(),
        stop=stop,
        poll_timeout_seconds=0.1,
    )
    assert in_flight == {}


def test_run_result_drain_refreshes_timestamp_on_claimed_ok(tmp_path: Path) -> None:
    original_ts = time.time() - 100.0
    in_flight: dict[str, float] = {'a': original_ts}
    rq: ResultQueue = mp.Queue()
    rq.put(ClaimedOk(issue_id='a'))
    rq.put(None)
    stop = threading.Event()
    run_result_drain(
        result_queue=rq,
        in_flight=in_flight,
        in_flight_lock=threading.Lock(),
        stop=stop,
        poll_timeout_seconds=0.1,
    )
    assert in_flight['a'] > original_ts
