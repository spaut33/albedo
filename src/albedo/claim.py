"""Linear-assignee claim with read-after-write verification.

Each worker authenticates as a distinct Linear user (`Agent 1`, `Agent 2`,
...), so `update_issue(assignee=<user>)` writes a value that's unique per
worker. The race-detection logic is then straightforward: write, sleep
briefly, re-read, and if the assignee is no longer ours, someone else won.

The earlier design also pushed an empty git ref as a second lock — that was
needed when all workers shared a single Linear user and couldn't
disambiguate. With per-agent users, the ref-push step doesn't help and
actively hurts the Reviewer path (the branch already exists from Coder).

Branch creation has moved into `ensure_worktree`, which is the right place
for it: the Coder creates the work branch as part of materialising the
worktree; the Reviewer reuses the existing branch.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from albedo.linear_client import Issue, IssueUpdate, LinearClient
from albedo.worktree import branch_for_issue

DEFAULT_VERIFICATION_SLEEP_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class ClaimResult:
    issue: Issue
    branch: str


class ClaimError(RuntimeError):
    """Raised when claim setup fails for non-conflict reasons (network etc.)."""


def try_claim(
    *,
    linear: LinearClient,
    issue: Issue,
    agent_user_id: str,
    verification_sleep_seconds: float = DEFAULT_VERIFICATION_SLEEP_SECONDS,
) -> ClaimResult | None:
    """Attempt to claim `issue` for the current worker.

    Returns a `ClaimResult` if our assignee write survived the race window.
    Returns `None` if another worker overwrote our value (in which case we
    leave the assignee field alone — whoever overwrote us is the new
    legitimate owner; un-setting would just steal the lock from them).
    """
    branch = branch_for_issue(issue.identifier)

    if issue.assignee_id is not None:
        return None

    linear.update_issue(issue.id, IssueUpdate(assignee_id=agent_user_id))
    if verification_sleep_seconds > 0:
        time.sleep(verification_sleep_seconds)
    fresh = linear.get_issue(issue.id)
    if fresh.assignee_id != agent_user_id:
        return None
    return ClaimResult(issue=fresh, branch=branch)


def release_claim(
    *,
    linear: LinearClient,
    issue: Issue,
) -> None:
    """Un-assign the issue. Used when the worker decides to bail."""
    linear.update_issue(issue.id, IssueUpdate(unset_assignee=True))
