"""Messages exchanged between the supervisor poller and worker processes.

The poller puts `CandidateMsg` onto the dispatch queue; workers consume,
attempt `try_claim`, run the role, and post `ResultMsg` back so the
supervisor can keep its `in_flight` set tight. All messages are frozen
dataclasses with primitive fields so they pickle cleanly across the
`mp.Queue` boundary.
"""

from __future__ import annotations

import multiprocessing as mp
from dataclasses import dataclass

from albedo.linear_client import Issue


@dataclass(frozen=True, slots=True)
class CandidateMsg:
    """A candidate issue offered to one worker."""

    issue: Issue
    offered_at_unix: float


@dataclass(frozen=True, slots=True)
class ClaimedOk:
    issue_id: str


@dataclass(frozen=True, slots=True)
class ClaimLost:
    issue_id: str


@dataclass(frozen=True, slots=True)
class TaskDone:
    """Worker finished with this issue (success or failure)."""

    issue_id: str


type ResultMsg = ClaimedOk | ClaimLost | TaskDone

# `None` is the supervisor → worker shutdown sentinel on the dispatch queue
# and the worker → supervisor shutdown sentinel on the result queue.
type DispatchQueue = mp.Queue[CandidateMsg | None]
type ResultQueue = mp.Queue[ResultMsg | None]
