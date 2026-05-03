"""Tests for the streaming claude subprocess wrapper.

We avoid invoking the real `claude` CLI by monkeypatching `subprocess.Popen`
with a fake that yields canned JSON-line events.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from albedo import claude_runner
from albedo.claude_runner import (
    ClaudeRunError,
    ClaudeRunResult,
    spawn_claude,
)
from albedo.stream_parser import StreamSnapshot


class _FakeStdout:
    """Iterable mimicking a text stream produced by Popen(stdout=PIPE)."""

    def __init__(self, lines: Iterable[str]) -> None:
        self._iter = iter(lines)

    def readline(self) -> str:
        try:
            line = next(self._iter)
        except StopIteration:
            return ''
        if not line.endswith('\n'):
            line += '\n'
        return line


class _FakeStderr:
    def __iter__(self) -> Iterator[str]:
        return iter(())


class _FakePopen:
    """Drop-in replacement for `subprocess.Popen` that scripts a stream."""

    def __init__(
        self,
        events: list[Mapping[str, Any]],
        *,
        returncode: int = 0,
        captured_args: list[Mapping[str, object]] | None = None,
    ) -> None:
        self._events = events
        self._returncode = returncode
        self._captured_args = captured_args
        self.stdout = _FakeStdout(json.dumps(e) for e in events)
        self.stderr = _FakeStderr()
        self.pid = 12345
        self.returncode: int | None = None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = self._returncode
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _install_fake_popen(
    monkeypatch: pytest.MonkeyPatch,
    events: list[Mapping[str, Any]],
    *,
    returncode: int = 0,
    captured: list[Mapping[str, object]] | None = None,
) -> None:
    def fake_popen(*args: object, **kwargs: object) -> _FakePopen:
        if captured is not None:
            captured.append({'args': args, 'kwargs': kwargs})
        return _FakePopen(events, returncode=returncode, captured_args=captured)

    monkeypatch.setattr(claude_runner.subprocess, 'Popen', fake_popen)


def _result_event(
    *,
    is_error: bool = False,
    result: str = 'ok',
    cost: float = 0.0,
    usage: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'type': 'result',
        'is_error': is_error,
        'result': result,
        'total_cost_usd': cost,
    }
    if usage is not None:
        payload['usage'] = dict(usage)
    return payload


def test_spawn_claude_parses_terminal_result(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[Mapping[str, Any]] = [
        {'type': 'system', 'subtype': 'init'},
        {
            'type': 'assistant',
            'message': {
                'content': [{'type': 'text', 'text': 'thinking…'}],
                'usage': {'input_tokens': 10, 'output_tokens': 5},
            },
        },
        _result_event(
            result='all good',
            usage={'input_tokens': 10, 'output_tokens': 5},
        ),
    ]
    _install_fake_popen(monkeypatch, events)

    result = spawn_claude(
        'do',
        cwd=Path('/tmp'),
        allowed_tools=['Read'],
        mcp_config_path=None,
        max_turns=5,
        timeout_seconds=30,
    )

    assert isinstance(result, ClaudeRunResult)
    assert result.is_error is False
    assert result.result_text == 'all good'
    assert result.usage == {'input_tokens': 10, 'output_tokens': 5}
    assert result.timed_out is False


def test_spawn_claude_invokes_callback_per_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Mapping[str, Any]] = [
        {'type': 'system', 'subtype': 'init'},
        {
            'type': 'assistant',
            'message': {
                'content': [
                    {'type': 'tool_use', 'name': 'Edit', 'input': {'file_path': 'a.py'}}
                ],
            },
        },
        _result_event(),
    ]
    _install_fake_popen(monkeypatch, events)

    seen: list[tuple[str, int]] = []

    def cb(event: Mapping[str, Any], snapshot: StreamSnapshot) -> None:
        etype = event.get('type')
        seen.append((str(etype), snapshot.tool_use_count))

    result = spawn_claude(
        'p',
        cwd=Path('/tmp'),
        allowed_tools=[],
        mcp_config_path=None,
        max_turns=5,
        timeout_seconds=30,
        on_event=cb,
    )

    assert result.is_error is False
    types = [t for t, _ in seen]
    assert types == ['system', 'assistant', 'result']
    # Tool count flips to 1 on the assistant event with the tool_use block.
    assert seen[1][1] == 1


def test_spawn_claude_writes_transcript_when_dir_provided(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[Mapping[str, Any]] = [
        {'type': 'system', 'subtype': 'init'},
        _result_event(result='ok'),
    ]
    _install_fake_popen(monkeypatch, events)

    transcript_dir = tmp_path / 'transcripts'

    result = spawn_claude(
        'p',
        cwd=Path('/tmp'),
        allowed_tools=[],
        mcp_config_path=None,
        max_turns=5,
        timeout_seconds=30,
        transcript_dir=transcript_dir,
        transcript_basename='LIN-1',
    )

    assert result.transcript_path is not None
    assert result.transcript_path.exists()
    lines = result.transcript_path.read_text(encoding='utf-8').splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])['type'] == 'system'
    assert json.loads(lines[1])['type'] == 'result'


def test_spawn_claude_assembles_args(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[Mapping[str, object]] = []
    _install_fake_popen(
        monkeypatch,
        [_result_event()],
        captured=captured,
    )

    spawn_claude(
        'hello',
        cwd=Path('/work'),
        allowed_tools=['Read', 'Bash(git:*)'],
        mcp_config_path=Path('/cfg/mcp.json'),
        max_turns=42,
        timeout_seconds=60,
    )

    args_tuple = cast('tuple[object, ...]', captured[0]['args'])
    raw_cmd = args_tuple[0]
    assert isinstance(raw_cmd, list)
    cmd = cast('list[str]', raw_cmd)
    assert cmd[:3] == ['claude', '-p', 'hello']
    assert '--output-format' in cmd
    assert cmd[cmd.index('--output-format') + 1] == 'stream-json'
    assert '--verbose' in cmd
    assert '--max-turns' in cmd and '42' in cmd
    assert '--allowed-tools' in cmd
    idx = cmd.index('--allowed-tools')
    assert cmd[idx + 1] == 'Read,Bash(git:*)'
    assert '--mcp-config' in cmd and '/cfg/mcp.json' in cmd
    assert '--permission-mode' not in cmd


def test_spawn_claude_includes_model_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Mapping[str, object]] = []
    _install_fake_popen(
        monkeypatch,
        [_result_event()],
        captured=captured,
    )

    spawn_claude(
        'hello',
        cwd=Path('/work'),
        allowed_tools=['Read'],
        mcp_config_path=None,
        max_turns=10,
        timeout_seconds=60,
        model='claude-haiku-4-5',
    )

    args_tuple = cast('tuple[object, ...]', captured[0]['args'])
    raw_cmd = args_tuple[0]
    assert isinstance(raw_cmd, list)
    cmd = cast('list[str]', raw_cmd)
    assert '--model' in cmd
    assert cmd[cmd.index('--model') + 1] == 'claude-haiku-4-5'


def test_spawn_claude_omits_model_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Mapping[str, object]] = []
    _install_fake_popen(
        monkeypatch,
        [_result_event()],
        captured=captured,
    )

    spawn_claude(
        'hello',
        cwd=Path('/work'),
        allowed_tools=['Read'],
        mcp_config_path=None,
        max_turns=10,
        timeout_seconds=60,
    )

    args_tuple = cast('tuple[object, ...]', captured[0]['args'])
    raw_cmd = args_tuple[0]
    assert isinstance(raw_cmd, list)
    cmd = cast('list[str]', raw_cmd)
    assert '--model' not in cmd


def test_spawn_claude_includes_permission_mode_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Mapping[str, object]] = []
    _install_fake_popen(
        monkeypatch,
        [_result_event()],
        captured=captured,
    )

    spawn_claude(
        'hello',
        cwd=Path('/work'),
        allowed_tools=['Read'],
        mcp_config_path=None,
        max_turns=10,
        timeout_seconds=60,
        permission_mode='plan',
    )

    args_tuple = cast('tuple[object, ...]', captured[0]['args'])
    raw_cmd = args_tuple[0]
    assert isinstance(raw_cmd, list)
    cmd = cast('list[str]', raw_cmd)
    assert '--permission-mode' in cmd
    assert cmd[cmd.index('--permission-mode') + 1] == 'plan'


def test_spawn_claude_raises_when_no_result_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Mapping[str, Any]] = [
        {'type': 'system', 'subtype': 'init'},
    ]
    _install_fake_popen(monkeypatch, events, returncode=1)

    with pytest.raises(ClaudeRunError, match='no result event'):
        spawn_claude(
            'p',
            cwd=Path('/tmp'),
            allowed_tools=[],
            mcp_config_path=None,
            max_turns=5,
            timeout_seconds=30,
        )


def test_spawn_claude_passes_extra_env(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[Mapping[str, object]] = []
    _install_fake_popen(
        monkeypatch,
        [_result_event()],
        captured=captured,
    )

    spawn_claude(
        'p',
        cwd=Path('/tmp'),
        allowed_tools=[],
        mcp_config_path=None,
        max_turns=5,
        timeout_seconds=30,
        extra_env={'FOO': 'bar'},
    )

    raw_kwargs = captured[0]['kwargs']
    assert isinstance(raw_kwargs, dict)
    kwargs = cast('dict[str, object]', raw_kwargs)
    env = kwargs['env']
    assert isinstance(env, dict)
    env_typed = cast('dict[str, str]', env)
    assert env_typed['FOO'] == 'bar'


def test_spawn_claude_inherits_exit_code_when_is_error_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Mapping[str, Any]] = [
        {'type': 'result', 'result': 'x'},  # no is_error key
    ]
    _install_fake_popen(monkeypatch, events, returncode=2)

    result = spawn_claude(
        'p',
        cwd=Path('/tmp'),
        allowed_tools=[],
        mcp_config_path=None,
        max_turns=5,
        timeout_seconds=30,
    )
    assert result.is_error is True
    assert result.exit_code == 2


def test_spawn_claude_tolerates_malformed_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage lines from the CLI must not crash the spawn loop."""

    class _MixedStdout:
        def __init__(self, lines: list[str]) -> None:
            self._iter = iter(lines)

        def readline(self) -> str:
            try:
                line = next(self._iter)
            except StopIteration:
                return ''
            return line if line.endswith('\n') else line + '\n'

    class _Popen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self.stdout = _MixedStdout(
                [
                    'this is not json',
                    json.dumps(_result_event(result='done')),
                ]
            )
            self.stderr = _FakeStderr()
            self.pid = 1
            self.returncode: int | None = None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(claude_runner.subprocess, 'Popen', _Popen)

    result = spawn_claude(
        'p',
        cwd=Path('/tmp'),
        allowed_tools=[],
        mcp_config_path=None,
        max_turns=5,
        timeout_seconds=30,
    )
    assert result.result_text == 'done'


def test_spawn_claude_timeout_kills_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When wallclock exceeds timeout we kill the process group."""

    class _NeverEndingStdout:
        def readline(self) -> str:
            # Never yields the result event — simulates a hung claude.
            return json.dumps({'type': 'system', 'subtype': 'init'}) + '\n'

    class _HangPopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self.stdout = _NeverEndingStdout()
            self.stderr = _FakeStderr()
            self.pid = os.getpid()  # so killpg targets a real pgid
            self.returncode: int | None = None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            if self.returncode is None:
                self.returncode = -15
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(claude_runner.subprocess, 'Popen', _HangPopen)

    killed: list[int] = []

    def fake_killpg(pid: int, sig: int) -> None:
        killed.append(sig)

    monkeypatch.setattr(claude_runner.os, 'killpg', fake_killpg)

    result = spawn_claude(
        'p',
        cwd=Path('/tmp'),
        allowed_tools=[],
        mcp_config_path=None,
        max_turns=5,
        timeout_seconds=0,
    )

    assert result.timed_out is True
    assert result.is_error is True
    assert 'timed out' in result.result_text
    assert killed  # SIGTERM (and possibly SIGKILL) attempted


def test_spawn_claude_raises_on_invalid_json_when_stream_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No events at all → no result → ClaudeRunError."""
    _install_fake_popen(monkeypatch, [], returncode=1)

    with pytest.raises(ClaudeRunError):
        spawn_claude(
            'p',
            cwd=Path('/tmp'),
            allowed_tools=[],
            mcp_config_path=None,
            max_turns=5,
            timeout_seconds=30,
        )
