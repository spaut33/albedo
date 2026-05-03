"""Tests for the pure CI-redispatch utilities."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from albedo.ci_redispatch import (
    LAST_CI_RUN_STATE_FILE,
    LAST_CI_RUN_STATE_VERSION,
    extract_linear_identifier,
    format_ci_failure_comment,
    load_last_ci_run,
    save_last_ci_run,
    seed_first_observation,
    truncate_log_tail,
)


class TestExtractLinearIdentifier:
    def test_bare_identifier(self) -> None:
        assert extract_linear_identifier('AI-58') == 'AI-58'

    def test_task_prefix(self) -> None:
        assert extract_linear_identifier('task/AI-58') == 'AI-58'

    def test_lowercase_uppercased(self) -> None:
        assert extract_linear_identifier('task/ai-58-add-utils') == 'AI-58'

    def test_mixed_case(self) -> None:
        assert extract_linear_identifier('feature/Ai-12') == 'AI-12'

    def test_no_identifier_returns_none(self) -> None:
        assert extract_linear_identifier('main') is None

    def test_only_digits_returns_none(self) -> None:
        assert extract_linear_identifier('release/2026-05') is None

    def test_only_letters_returns_none(self) -> None:
        assert extract_linear_identifier('feature/foo') is None

    def test_first_match_wins_when_multiple(self) -> None:
        assert extract_linear_identifier('task/AI-58-then-AI-12-fix') == 'AI-58'

    def test_first_match_wins_across_prefixes(self) -> None:
        assert extract_linear_identifier('eng-9/AI-58-thing') == 'ENG-9'

    def test_empty_string(self) -> None:
        assert extract_linear_identifier('') is None

    def test_word_boundary_ignores_substring(self) -> None:
        # `notai-58` does not start at a word boundary for the alpha run,
        # but `\b` still anchors before `notai`, so this DOES match the
        # whole alpha run. The point of the boundary is that `1ai-58`
        # would not match — the alpha cluster has to start at a boundary.
        assert extract_linear_identifier('1ai-58') is None


class TestTruncateLogTail:
    def test_empty_log_returns_empty(self) -> None:
        assert truncate_log_tail('') == ''

    def test_log_shorter_than_max_returned_unchanged(self) -> None:
        log = '\n'.join(f'line {i}' for i in range(50))
        assert truncate_log_tail(log, max_lines=200) == log

    def test_log_at_max_returned_unchanged(self) -> None:
        log = '\n'.join(f'line {i}' for i in range(200))
        assert truncate_log_tail(log, max_lines=200) == log

    def test_log_longer_than_max_prepends_marker(self) -> None:
        log = '\n'.join(f'line {i}' for i in range(250))
        result = truncate_log_tail(log, max_lines=200)
        lines = result.split('\n')
        assert lines[0] == '… (50 earlier lines elided)'
        assert lines[1] == 'line 50'
        assert lines[-1] == 'line 249'
        assert len(lines) == 201

    def test_default_max_lines_is_200(self) -> None:
        log = '\n'.join(f'line {i}' for i in range(205))
        result = truncate_log_tail(log)
        assert result.startswith('… (5 earlier lines elided)')

    def test_single_line_log_unchanged(self) -> None:
        assert truncate_log_tail('only line') == 'only line'

    def test_custom_max_lines(self) -> None:
        log = '\n'.join(f'line {i}' for i in range(10))
        result = truncate_log_tail(log, max_lines=3)
        lines = result.split('\n')
        assert lines[0] == '… (7 earlier lines elided)'
        assert lines[1:] == ['line 7', 'line 8', 'line 9']


class TestFormatCiFailureComment:
    def test_shape(self) -> None:
        body = format_ci_failure_comment(
            workflow_name='CI',
            run_url='https://github.com/o/r/actions/runs/42',
            job_name='unit-tests',
            step_name='pytest',
            log_tail='E   AssertionError\n',
        )
        lines = body.split('\n')
        assert lines[0].startswith('**CI failure**')
        assert 'CI' in lines[0]
        assert 'https://github.com/o/r/actions/runs/42' in lines[0]
        assert lines[1].startswith('Failed job:')
        assert '`unit-tests`' in lines[1]
        assert '`pytest`' in lines[1]
        assert '```log' in body
        assert 'E   AssertionError' in body
        assert body.rstrip().endswith('```')

    def test_log_block_wraps_verbatim(self) -> None:
        body = format_ci_failure_comment(
            workflow_name='ci.yml',
            run_url='https://x',
            job_name='j',
            step_name='s',
            log_tail='line-a\nline-b',
        )
        assert '```log\nline-a\nline-b\n```' in body

    def test_empty_log_tail_still_renders_fence(self) -> None:
        body = format_ci_failure_comment(
            workflow_name='ci',
            run_url='https://x',
            job_name='j',
            step_name='s',
            log_tail='',
        )
        assert '```log\n\n```' in body


class TestLoadSaveLastCiRun:
    def test_load_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_last_ci_run(tmp_path) == {}

    def test_round_trip(self, tmp_path: Path) -> None:
        mapping = {'uuid-a': 'run-1', 'uuid-b': 'run-2'}
        save_last_ci_run(tmp_path, mapping)
        loaded = load_last_ci_run(tmp_path)
        assert loaded == mapping

    def test_save_creates_state_file_with_version_key(self, tmp_path: Path) -> None:
        save_last_ci_run(tmp_path, {'uuid-a': 'run-1'})
        path = tmp_path / LAST_CI_RUN_STATE_FILE
        assert path.exists()
        payload = json.loads(path.read_text(encoding='utf-8'))
        assert payload['version'] == LAST_CI_RUN_STATE_VERSION
        assert payload['runs'] == {'uuid-a': 'run-1'}

    def test_save_creates_parent_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / 'state' / 'sub'
        save_last_ci_run(nested, {'uuid-a': 'run-1'})
        assert (nested / LAST_CI_RUN_STATE_FILE).exists()

    def test_repeated_saves_idempotent(self, tmp_path: Path) -> None:
        mapping = {'uuid-a': 'run-1', 'uuid-b': 'run-2'}
        save_last_ci_run(tmp_path, mapping)
        first = (tmp_path / LAST_CI_RUN_STATE_FILE).read_bytes()
        save_last_ci_run(tmp_path, mapping)
        second = (tmp_path / LAST_CI_RUN_STATE_FILE).read_bytes()
        assert first == second

    def test_save_order_independent(self, tmp_path: Path) -> None:
        save_last_ci_run(tmp_path, {'b': '2', 'a': '1'})
        first = (tmp_path / LAST_CI_RUN_STATE_FILE).read_bytes()
        save_last_ci_run(tmp_path, {'a': '1', 'b': '2'})
        second = (tmp_path / LAST_CI_RUN_STATE_FILE).read_bytes()
        assert first == second

    def test_corrupt_json_returns_empty_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / LAST_CI_RUN_STATE_FILE
        path.write_text('{not valid json', encoding='utf-8')
        with caplog.at_level(logging.WARNING, logger='albedo.ci_redispatch'):
            assert load_last_ci_run(tmp_path) == {}
        assert any('corrupt' in rec.message for rec in caplog.records)

    def test_wrong_version_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / LAST_CI_RUN_STATE_FILE
        path.write_text(
            json.dumps({'version': 999, 'runs': {'a': '1'}}), encoding='utf-8'
        )
        assert load_last_ci_run(tmp_path) == {}

    def test_non_dict_payload_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / LAST_CI_RUN_STATE_FILE
        path.write_text('[1, 2, 3]', encoding='utf-8')
        assert load_last_ci_run(tmp_path) == {}

    def test_runs_not_a_dict_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / LAST_CI_RUN_STATE_FILE
        path.write_text(
            json.dumps({'version': LAST_CI_RUN_STATE_VERSION, 'runs': []}),
            encoding='utf-8',
        )
        assert load_last_ci_run(tmp_path) == {}

    def test_save_then_overwrite_replaces_mapping(self, tmp_path: Path) -> None:
        save_last_ci_run(tmp_path, {'a': '1'})
        save_last_ci_run(tmp_path, {'a': '2', 'b': '3'})
        assert load_last_ci_run(tmp_path) == {'a': '2', 'b': '3'}


class TestSeedFirstObservation:
    def test_seeds_when_unseen(self) -> None:
        state: dict[str, str] = {}
        seeded = seed_first_observation(state, 'uuid-a', 'run-1')
        assert seeded is True
        assert state == {'uuid-a': 'run-1'}

    def test_does_not_seed_when_already_known(self) -> None:
        state = {'uuid-a': 'run-1'}
        seeded = seed_first_observation(state, 'uuid-a', 'run-2')
        assert seeded is False
        assert state == {'uuid-a': 'run-1'}

    def test_seeds_per_issue_independently(self) -> None:
        state: dict[str, str] = {'uuid-a': 'run-1'}
        seeded = seed_first_observation(state, 'uuid-b', 'run-9')
        assert seeded is True
        assert state == {'uuid-a': 'run-1', 'uuid-b': 'run-9'}
