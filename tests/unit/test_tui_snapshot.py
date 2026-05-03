"""Tests for the TUI snapshot aggregator and log tailer."""

from __future__ import annotations

import json
from pathlib import Path

from albedo.status_writer import StatusWriter, status_path
from albedo.tui.snapshot import (
    LogTailer,
    aggregate,
    discover_agent_ids,
    load_linear_queue,
)


def _seed_log(log_dir: Path, agent_id: str, lines: list[dict[str, object]]) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f'agent-{agent_id}.log'
    with path.open('a', encoding='utf-8') as f:
        for line in lines:
            f.write(json.dumps(line) + '\n')
    return path


def test_log_tailer_seeds_at_eof_and_picks_up_appended_lines(tmp_path: Path) -> None:
    log_dir = tmp_path / 'logs'
    _seed_log(log_dir, '1', [{'event': 'old', 'agent': '1', 'level': 'info'}])
    tailer = LogTailer(log_dir)
    # Seeded at EOF — first poll yields nothing.
    assert tailer.poll() == []
    # New line is captured on next poll.
    _seed_log(log_dir, '1', [{'event': 'new', 'agent': '1', 'level': 'info'}])
    new = tailer.poll()
    assert len(new) == 1
    assert new[0].message == 'new'


def test_log_tailer_handles_rotation(tmp_path: Path) -> None:
    log_dir = tmp_path / 'logs'
    _seed_log(log_dir, '1', [{'event': 'before', 'agent': '1'}])
    tailer = LogTailer(log_dir)
    tailer.poll()  # noop, EOF seeded
    target = log_dir / 'agent-1.log'
    target.unlink()
    _seed_log(log_dir, '1', [{'event': 'after-rotation', 'agent': '1'}])
    fresh = tailer.poll()
    assert any(ev.message == 'after-rotation' for ev in fresh)


def test_load_linear_queue_handles_missing(tmp_path: Path) -> None:
    snap = load_linear_queue(tmp_path)
    assert snap.counts == {}
    assert snap.updated_at == 0.0


def test_load_linear_queue_parses_valid_json(tmp_path: Path) -> None:
    (tmp_path / 'linear_queue.json').write_text(
        json.dumps({'counts': {'Backlog': 3, 'Review': 1}, 'updated_at': 12.5}),
        encoding='utf-8',
    )
    snap = load_linear_queue(tmp_path)
    assert snap.counts == {'Backlog': 3, 'Review': 1}
    assert snap.updated_at == 12.5


def test_discover_agent_ids_pads_to_expected(tmp_path: Path) -> None:
    StatusWriter(state_dir=tmp_path, agent_id='1').remove()
    # Even with no files, expected=3 still yields three slots.
    ids = discover_agent_ids(tmp_path, expected=3)
    assert ids == ['1', '2', '3']


def test_discover_agent_ids_picks_up_status_and_heartbeat_files(
    tmp_path: Path,
) -> None:
    (tmp_path / 'agent-1.heartbeat').write_text('', encoding='utf-8')
    StatusWriter(state_dir=tmp_path, agent_id='2')
    ids = discover_agent_ids(tmp_path)
    assert '1' in ids and '2' in ids


def test_aggregate_collects_status_and_events(tmp_path: Path) -> None:
    log_dir = tmp_path / 'logs'
    log_dir.mkdir()
    writer = StatusWriter(state_dir=tmp_path, agent_id='1')
    writer.set_issue(issue_id='id', identifier='LIN-1', title='t', role='CODER')
    tailer = LogTailer(log_dir)
    # Seed an event after the tailer initialised so it appears.
    _seed_log(log_dir, '1', [{'event': 'claimed LIN-1', 'agent': '1', 'level': 'info'}])
    snap = aggregate(
        state_dir=tmp_path,
        log_tailer=tailer,
        expected_workers=1,
        project_name='sample',
    )
    assert snap.project_name == 'sample'
    assert len(snap.workers) == 1
    view = snap.workers[0]
    assert view.status is not None
    assert view.status.role == 'CODER'
    assert any(ev.message == 'claimed LIN-1' for ev in snap.events)
    # Cleanup so the next run does not see this status file.
    Path(status_path(tmp_path, '1')).unlink(missing_ok=True)
