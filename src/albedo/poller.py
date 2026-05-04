"""Single-poller fan-out: the supervisor's discovery loop.

Workers used to poll Linear independently — at N workers that's N
identical `list_pickup_issues` queries per tick plus N `try_claim`
attempts on the same hot row. The poller collapses that to a single
query per tick, hands candidates out via `mp.Queue`, and lets each
candidate land on exactly one worker (because `mp.Queue.get` is atomic).

The poller also publishes the TUI queue snapshot (formerly per-worker)
and consults the rolling-window throttle: if the orchestrator is over
its token budget, no offers go out at all.

A worker crash is not the poller's problem — `recover_stale_claims`
still un-assigns abandoned Linear issues, and the `offer_ttl_seconds`
fallback drops stuck `in_flight` entries so the next tick can re-offer.
"""

from __future__ import annotations

import logging
import threading
import time
from queue import Empty, Full

from albedo.config import OrchestratorConfig
from albedo.dispatch_messages import (
    CandidateMsg,
    ClaimedOk,
    DispatchQueue,
    ResultQueue,
)
from albedo.linear_client import LinearClient
from albedo.linear_queue_writer import publish_queue
from albedo.usage import UsageLedger
from albedo.usage_limit import format_pause_summary, is_paused
from albedo.worker import (
    EXCLUDE_LABELS,
    PICKUP_STATES,
    filter_dispatchable,
)

log = logging.getLogger(__name__)

SUPERVISOR_AGENT_ID = '_supervisor'
THROTTLE_LOG_INTERVAL_SECONDS = 60.0
PAUSE_LOG_INTERVAL_SECONDS = 60.0
BACKOFF_FLOOR_SECONDS = 30
BACKOFF_CEILING_SECONDS = 300


class Poller:
    """One tick = one Linear query + best-effort fan-out to workers.

    `in_flight` is supervisor-local: issue.id → unix ts when the offer
    was put on the dispatch queue. The result-drain thread removes ids
    on `ResultMsg`; the poller drops anything older than
    `offer_ttl_seconds` so a crashed worker can't permanently park an
    issue.

    `in_flight_lock` is held when reading or mutating `in_flight` —
    the result-drain thread is the other writer.
    """

    def __init__(
        self,
        *,
        linear: LinearClient,
        config: OrchestratorConfig,
        team_id: str,
        dispatch_queue: DispatchQueue,
        in_flight: dict[str, float],
        in_flight_lock: threading.Lock,
        ledger: UsageLedger,
    ) -> None:
        self._linear = linear
        self._config = config
        self._team_id = team_id
        self._dispatch_queue = dispatch_queue
        self._in_flight = in_flight
        self._in_flight_lock = in_flight_lock
        self._ledger = ledger
        self._last_throttle_log_at = 0.0
        self._last_pause_log_at = 0.0

    def tick(self) -> None:
        """One discovery + fan-out cycle.

        Side effects:
          * trims `in_flight` to drop expired offers (TTL recovery)
          * if throttled: logs (rate-limited) and returns early
          * else: queries Linear, publishes TUI snapshot, offers each
            unseen candidate via `put_nowait`. On `Full` we stop offering
            for this tick; the worker will catch up before the next.
        """
        now = time.time()
        self._evict_stale_in_flight(now)

        if self._is_paused(now):
            return

        if self._is_throttled(now):
            return

        try:
            candidates = self._linear.list_pickup_issues(
                self._team_id,
                list(PICKUP_STATES),
                exclude_labels=list(EXCLUDE_LABELS),
                project_id=self._config.linear.project_id,
            )
        except Exception:
            raise

        candidates = filter_dispatchable(candidates)

        try:
            queue_snapshot = self._linear.list_active_issues(
                self._team_id, project_id=self._config.linear.project_id
            )
        except Exception as exc:
            log.warning('queue snapshot fetch failed: %s', exc)
            queue_snapshot = candidates
        publish_queue(
            self._config.state_dir,
            agent_id=SUPERVISOR_AGENT_ID,
            issues=queue_snapshot,
            pickup_states=PICKUP_STATES,
        )

        offered = 0
        full_break = False
        with self._in_flight_lock:
            for issue in candidates:
                if issue.id in self._in_flight:
                    continue
                try:
                    self._dispatch_queue.put_nowait(
                        CandidateMsg(issue=issue, offered_at_unix=now)
                    )
                except Full:
                    full_break = True
                    break
                self._in_flight[issue.id] = now
                offered += 1

            in_flight_count = len(self._in_flight)

        log.info(
            'poll: candidates=%d offered=%d in_flight=%d%s',
            len(candidates),
            offered,
            in_flight_count,
            ' (queue full — pausing offers this tick)' if full_break else '',
        )

    def _evict_stale_in_flight(self, now: float) -> None:
        ttl = float(self._config.dispatch.offer_ttl_seconds)
        with self._in_flight_lock:
            stale = [
                issue_id for issue_id, ts in self._in_flight.items() if now - ts > ttl
            ]
            for issue_id in stale:
                del self._in_flight[issue_id]
        if stale:
            log.info(
                'poll: dropped %d stale in_flight offer(s) past TTL %ds',
                len(stale),
                int(ttl),
            )

    def _is_paused(self, now: float) -> bool:
        """Return True when a Claude usage-limit pause is active.

        While paused, the poller emits no offers — workers stay idle on the
        queue until the wall-clock catches up. Logs at most once per minute
        so a multi-hour pause doesn't flood the log.
        """
        state = is_paused(self._config.state_dir, now_unix=now)
        if state is None:
            return False
        if now - self._last_pause_log_at >= PAUSE_LOG_INTERVAL_SECONDS:
            log.info(
                'poll: %s; no offers this tick (%.0fs remaining)',
                format_pause_summary(state),
                max(0.0, state.until_unix - now),
            )
            self._last_pause_log_at = now
        return True

    def _is_throttled(self, now: float) -> bool:
        if not self._ledger.should_throttle(
            token_cap=self._config.usage.rolling_window_token_cap,
            window_seconds=self._config.usage.rolling_window_seconds,
        ):
            return False
        if now - self._last_throttle_log_at >= THROTTLE_LOG_INTERVAL_SECONDS:
            log.info(
                'poll: throttled (rolling-window token cap reached); '
                'no offers this tick'
            )
            self._last_throttle_log_at = now
        return True


def run_result_drain(
    *,
    result_queue: ResultQueue,
    in_flight: dict[str, float],
    in_flight_lock: threading.Lock,
    stop: threading.Event,
    poll_timeout_seconds: float = 1.0,
) -> None:
    """Update `in_flight` from worker → supervisor `ResultMsg`.

    `ClaimedOk` refreshes the offer timestamp (the work is alive, so
    the TTL clock should restart from the moment claim succeeded).
    `ClaimLost` and `TaskDone` drop the id — claim was lost to a race
    or human, or the worker is done with this issue.

    Stops on `None` sentinel or when `stop` is set.
    """
    while not stop.is_set():
        try:
            msg = result_queue.get(timeout=poll_timeout_seconds)
        except Empty:
            continue
        if msg is None:
            log.info('result-drain: shutdown sentinel received')
            break
        with in_flight_lock:
            if isinstance(msg, ClaimedOk):
                if msg.issue_id in in_flight:
                    in_flight[msg.issue_id] = time.time()
            else:
                in_flight.pop(msg.issue_id, None)


def run_poller(
    *,
    poller: Poller,
    poll_interval_seconds: int,
    stop: threading.Event,
) -> None:
    """Drive `poller.tick()` until `stop` is set.

    Linear failures get bounded backoff (30s → 300s, mirroring the worker
    poll-fail backoff at the previous worker.run_loop). On success the
    backoff resets so a single hiccup doesn't permanently slow the loop.
    The sleep is interruptible via `stop.wait` so shutdown stays snappy.
    """
    attempt = 0
    while not stop.is_set():
        try:
            poller.tick()
            attempt = 0
        except Exception as exc:
            attempt += 1
            backoff = min(
                BACKOFF_CEILING_SECONDS,
                max(BACKOFF_FLOOR_SECONDS, poll_interval_seconds * (attempt + 1) * 2),
            )
            log.warning(
                'poll tick failed (attempt %d): %s; backing off %ds',
                attempt,
                exc,
                backoff,
            )
            stop.wait(backoff)
            continue
        stop.wait(poll_interval_seconds)
