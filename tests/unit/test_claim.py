"""Tests for the Linear-assignee claim with read-after-write verification."""

from __future__ import annotations

import pytest

from albedo import claim as claim_mod
from albedo.claim import ClaimResult, release_claim, try_claim
from albedo.linear_client import Issue, IssueUpdate


def _issue(assignee_id: str | None = None) -> Issue:
    return Issue(
        id='uuid-1',
        identifier='AI-5',
        title='X',
        description='',
        state_id='s',
        state_name='Backlog',
        assignee_id=assignee_id,
        label_ids=(),
        label_names=(),
        parent_id=None,
        branch_name='',
    )


class _FakeLinear:
    """Drop-in for the bits of `LinearClient` the claim path uses."""

    def __init__(self, *, on_get: str | None = 'agent-user') -> None:
        self.updates: list[IssueUpdate] = []
        self._on_get = on_get

    def update_issue(self, _issue_id: str, update: IssueUpdate) -> Issue:
        self.updates.append(update)
        return _issue(self._on_get)

    def get_issue(self, _identifier: str) -> Issue:
        return _issue(self._on_get)


def _patch_no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    def _noop(_s: float) -> None:
        return None

    monkeypatch.setattr(claim_mod.time, 'sleep', _noop)


@pytest.fixture(autouse=True)
def _no_sleep(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_no_sleep(monkeypatch)


def test_try_claim_success() -> None:
    fake = _FakeLinear(on_get='agent-user')
    result = try_claim(
        linear=fake,  # type: ignore[arg-type]
        issue=_issue(),
        agent_user_id='agent-user',
    )
    assert isinstance(result, ClaimResult)
    assert result.branch == 'task/ai-5'
    assert len(fake.updates) == 1
    assert fake.updates[0].assignee_id == 'agent-user'


def test_try_claim_returns_none_when_already_assigned() -> None:
    fake = _FakeLinear()
    result = try_claim(
        linear=fake,  # type: ignore[arg-type]
        issue=_issue(assignee_id='someone-else'),
        agent_user_id='agent-user',
    )
    assert result is None
    assert fake.updates == []


def test_try_claim_returns_none_when_overwritten() -> None:
    fake = _FakeLinear(on_get='other-user')
    result = try_claim(
        linear=fake,  # type: ignore[arg-type]
        issue=_issue(),
        agent_user_id='agent-user',
    )
    assert result is None
    # We did write our assignee, but did NOT roll it back: whoever overwrote
    # us is the legitimate new owner, and un-setting would steal their lock.
    assert len(fake.updates) == 1
    assert fake.updates[0].assignee_id == 'agent-user'


def test_release_claim_unsets_assignee() -> None:
    fake = _FakeLinear()
    release_claim(linear=fake, issue=_issue(assignee_id='agent-user'))  # type: ignore[arg-type]
    assert len(fake.updates) == 1
    assert fake.updates[0].unset_assignee is True
