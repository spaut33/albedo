"""Tests for the Linear queue snapshot writer."""

from __future__ import annotations

import json
from pathlib import Path

from albedo.linear_client import Issue
from albedo.linear_queue_writer import publish_queue, queue_path


def _issue(identifier: str, state: str) -> Issue:
    return Issue(
        id='id-' + identifier,
        identifier=identifier,
        title=identifier,
        description='',
        state_id='s',
        state_name=state,
        assignee_id=None,
        label_ids=(),
        label_names=(),
        parent_id=None,
        branch_name='task/' + identifier,
        incoming_relations=(),
    )


def test_publish_queue_writes_zero_rows_for_pickup_states(tmp_path: Path) -> None:
    publish_queue(
        tmp_path,
        agent_id='1',
        issues=[],
        pickup_states=('Triage', 'Backlog', 'Review'),
    )
    payload = json.loads(queue_path(tmp_path).read_text(encoding='utf-8'))
    assert payload['counts'] == {'Triage': 0, 'Backlog': 0, 'Review': 0}
    assert payload['agent_id'] == '1'


def test_publish_queue_counts_issues_by_state(tmp_path: Path) -> None:
    publish_queue(
        tmp_path,
        agent_id='2',
        issues=[
            _issue('LIN-1', 'Backlog'),
            _issue('LIN-2', 'Backlog'),
            _issue('LIN-3', 'Review'),
        ],
        pickup_states=('Triage', 'Backlog', 'Review'),
    )
    payload = json.loads(queue_path(tmp_path).read_text(encoding='utf-8'))
    assert payload['counts'] == {'Triage': 0, 'Backlog': 2, 'Review': 1}


def test_publish_queue_atomic_replace(tmp_path: Path) -> None:
    """Ensure the temp file does not linger and the target is fully written."""
    publish_queue(tmp_path, agent_id='1', issues=[], pickup_states=('Backlog',))
    assert queue_path(tmp_path).exists()
    assert not (tmp_path / 'linear_queue.json.tmp').exists()
