"""Smoke tests for structlog configuration."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from albedo.logging_setup import configure_logging


def test_configure_logging_emits_json_to_per_process_file(tmp_path: Path) -> None:
    log_path = configure_logging(agent_id='7', level='INFO', state_dir=tmp_path)
    try:
        logging.getLogger('test.scope').info(
            'hello structlog', extra={'issue': 'AI-42'}
        )
        for handler in logging.getLogger().handlers:
            handler.flush()

        assert log_path == tmp_path / 'logs' / 'agent-7.log'
        assert log_path.exists()
        lines = log_path.read_text(encoding='utf-8').strip().splitlines()
        assert lines, 'expected at least one log line'
        record = json.loads(lines[-1])
        assert record['event'] == 'hello structlog'
        assert record['agent'] == '7'
        assert record['level'] == 'info'
        assert record['logger'] == 'test.scope'
    finally:
        for handler in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(handler)
            handler.close()


def test_configure_logging_supervisor_filename(tmp_path: Path) -> None:
    log_path = configure_logging(agent_id=None, level='INFO', state_dir=tmp_path)
    try:
        assert log_path.name == 'supervisor.log'
        assert log_path.parent == tmp_path / 'logs'
    finally:
        for handler in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(handler)
            handler.close()
