"""Tests for the worker status snapshot writer/reader."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from albedo.status_writer import (
    PHASE_POLLING,
    PHASE_SPAWNING_CLAUDE,
    StatusWriter,
    read_status,
    status_path,
)
from albedo.stream_parser import StreamSnapshot


def test_initial_status_file_is_written(tmp_path: Path) -> None:
    writer = StatusWriter(state_dir=tmp_path, agent_id='1')
    path = status_path(tmp_path, '1')
    assert path.exists()
    payload = json.loads(path.read_text(encoding='utf-8'))
    assert payload['agent_id'] == '1'
    assert payload['phase'] == 'booting'
    writer.remove()


def test_display_name_persists_via_init_and_setter(tmp_path: Path) -> None:
    writer = StatusWriter(state_dir=tmp_path, agent_id='1', display_name='alice')
    payload = json.loads(status_path(tmp_path, '1').read_text(encoding='utf-8'))
    assert payload['display_name'] == 'alice'
    writer.set_display_name('bob')
    payload = json.loads(status_path(tmp_path, '1').read_text(encoding='utf-8'))
    assert payload['display_name'] == 'bob'


def test_set_phase_updates_file(tmp_path: Path) -> None:
    writer = StatusWriter(state_dir=tmp_path, agent_id='1')
    writer.set_phase(PHASE_POLLING)
    payload = json.loads(status_path(tmp_path, '1').read_text(encoding='utf-8'))
    assert payload['phase'] == PHASE_POLLING


def test_set_issue_records_role_and_clear_resets(tmp_path: Path) -> None:
    writer = StatusWriter(state_dir=tmp_path, agent_id='2')
    writer.set_issue(
        issue_id='uuid',
        identifier='LIN-1',
        title='do thing',
        role='CODER',
        worktree='/tmp/wt',
        branch='task/LIN-1',
    )
    payload = json.loads(status_path(tmp_path, '2').read_text(encoding='utf-8'))
    assert payload['role'] == 'CODER'
    assert payload['issue']['identifier'] == 'LIN-1'

    writer.clear_issue()
    payload = json.loads(status_path(tmp_path, '2').read_text(encoding='utf-8'))
    assert payload['issue'] is None
    assert payload['role'] == ''


def test_update_stream_serializes_snapshot(tmp_path: Path) -> None:
    writer = StatusWriter(state_dir=tmp_path, agent_id='3')
    writer.set_phase(PHASE_SPAWNING_CLAUDE)
    snap = StreamSnapshot()
    snap.feed(
        {
            'type': 'assistant',
            'message': {
                'content': [
                    {
                        'type': 'tool_use',
                        'name': 'Edit',
                        'input': {'file_path': 'a.py'},
                    }
                ],
                'usage': {'input_tokens': 10, 'output_tokens': 1},
            },
        }
    )
    writer.update_stream(snap)
    payload = json.loads(status_path(tmp_path, '3').read_text(encoding='utf-8'))
    stream = payload['stream']
    assert stream['turns'] == 1
    assert stream['tool_use_count'] == 1
    assert stream['last_tool_name'] == 'Edit'
    assert stream['last_tool_target'] == 'a.py'
    assert stream['input_tokens'] == 10
    assert len(stream['recent_tools']) == 1


def test_read_status_round_trips(tmp_path: Path) -> None:
    writer = StatusWriter(state_dir=tmp_path, agent_id='4')
    writer.set_issue(
        issue_id='id',
        identifier='LIN-9',
        title='t',
        role='REVIEWER',
        url='https://linear.app/acme/issue/LIN-9/t',
    )
    payload = json.loads(status_path(tmp_path, '4').read_text(encoding='utf-8'))
    assert payload['issue']['url'] == 'https://linear.app/acme/issue/LIN-9/t'
    status = read_status(status_path(tmp_path, '4'))
    assert status is not None
    assert status.role == 'REVIEWER'
    assert status.issue is not None
    assert status.issue.identifier == 'LIN-9'
    assert status.issue.url == 'https://linear.app/acme/issue/LIN-9/t'


def test_read_status_defaults_url_for_legacy_files(tmp_path: Path) -> None:
    """Status files written before the `url` field still load cleanly."""
    p = status_path(tmp_path, '6')
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                'agent_id': '6',
                'updated_at': 1.0,
                'phase': PHASE_POLLING,
                'phase_started_at': 1.0,
                'issue': {'id': 'id', 'identifier': 'LIN-1', 'title': 't'},
                'role': 'CODER',
                'stream': {},
            }
        ),
        encoding='utf-8',
    )
    status = read_status(p)
    assert status is not None
    assert status.issue is not None
    assert status.issue.url == ''


def test_read_status_missing_returns_none(tmp_path: Path) -> None:
    assert read_status(tmp_path / 'nope.json') is None


def test_read_status_malformed_returns_none(tmp_path: Path) -> None:
    p = tmp_path / 'bad.json'
    p.write_text('not json', encoding='utf-8')
    assert read_status(p) is None


def test_concurrent_writes_yield_valid_json(tmp_path: Path) -> None:
    """Atomic os.replace must mean readers never see a half-written file."""
    writer = StatusWriter(state_dir=tmp_path, agent_id='5')
    target = status_path(tmp_path, '5')
    stop = threading.Event()
    errors: list[str] = []

    def writer_loop() -> None:
        for i in range(500):
            writer.set_phase(PHASE_POLLING if i % 2 == 0 else PHASE_SPAWNING_CLAUDE)

    def reader_loop() -> None:
        while not stop.is_set():
            try:
                raw = target.read_text(encoding='utf-8')
            except FileNotFoundError:
                continue
            try:
                json.loads(raw)
            except json.JSONDecodeError as exc:
                errors.append(str(exc))
                return

    rt = threading.Thread(target=reader_loop, daemon=True)
    rt.start()
    writer_loop()
    stop.set()
    rt.join(timeout=2)
    assert not errors, f'reader saw torn writes: {errors[:3]}'
    writer.remove()


def test_remove_deletes_file(tmp_path: Path) -> None:
    writer = StatusWriter(state_dir=tmp_path, agent_id='6')
    p = status_path(tmp_path, '6')
    assert p.exists()
    writer.remove()
    assert not p.exists()
    # Idempotent: second remove is a no-op.
    writer.remove()
