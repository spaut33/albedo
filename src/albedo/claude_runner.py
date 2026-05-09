"""Subprocess wrapper around `claude -p` (the headless Claude Code CLI).

One spawn per task. Runs claude under `--output-format stream-json --verbose`
so the orchestrator can surface per-event progress (current tool call, token
usage, turn count) to the live TUI without waiting for the process to exit.
The full event stream is also persisted to `state/transcripts/<issue>-<ts>.jsonl`
for post-mortem debugging. The terminal `result` event is parsed into the
same `ClaudeRunResult` shape callers used to get from the buffered runner.

Side effects (issue moved, PR opened) are not inferred from stdout — callers
verify them through Linear/GitHub MCP after the spawn returns.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from albedo.stream_parser import StreamSnapshot

DEFAULT_CLI = 'claude'

StreamEventCallback = Callable[[Mapping[str, Any], StreamSnapshot], None]


class ClaudeRunError(RuntimeError):
    """Raised when the claude subprocess fails to run or returns junk."""


def _empty_int_map() -> Mapping[str, int]:
    return {}


def _empty_any_map() -> Mapping[str, Any]:
    return {}


@dataclass(frozen=True, slots=True)
class ClaudeRunResult:
    is_error: bool
    exit_code: int
    result_text: str
    total_cost_usd: float
    usage: Mapping[str, int] = field(default_factory=_empty_int_map)
    raw: Mapping[str, Any] = field(default_factory=_empty_any_map)
    timed_out: bool = False
    transcript_path: Path | None = None


def spawn_claude(
    prompt: str,
    *,
    cwd: Path,
    allowed_tools: list[str],
    mcp_config_path: Path | None,
    max_turns: int,
    timeout_seconds: int,
    cli: str = DEFAULT_CLI,
    extra_env: Mapping[str, str] | None = None,
    transcript_dir: Path | None = None,
    transcript_basename: str | None = None,
    on_event: StreamEventCallback | None = None,
    permission_mode: str | None = None,
    model: str | None = None,
) -> ClaudeRunResult:
    """Run a one-shot `claude -p` invocation and parse its event stream.

    Streams events as they arrive: each line of stdout is a JSON object,
    fed to the optional `on_event` callback (current event + running
    snapshot) and to a transcript file if `transcript_dir` is provided.
    The trailing `result` event yields `is_error`, `total_cost_usd`,
    `usage`, and the textual `result`.

    On wallclock timeout the process is killed and a `timed_out=True`
    result is returned (no exception) so the worker can move the issue
    to the blocker column rather than crashing.
    """
    args = _build_args(
        prompt,
        allowed_tools=allowed_tools,
        mcp_config_path=mcp_config_path,
        max_turns=max_turns,
        cli=cli,
        permission_mode=permission_mode,
        model=model,
    )
    transcript_path = _resolve_transcript_path(transcript_dir, transcript_basename)
    snapshot = StreamSnapshot()
    stderr_lines: list[str] = []

    proc = subprocess.Popen(
        args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=_merged_env(extra_env),
        start_new_session=True,
    )

    stderr_thread = threading.Thread(
        target=_drain_stream,
        args=(proc.stderr, stderr_lines),
        daemon=True,
    )
    stderr_thread.start()

    timed_out = False
    deadline = time.monotonic() + timeout_seconds

    transcript_file = None
    if transcript_path is not None:
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_file = transcript_path.open('w', encoding='utf-8')

    try:
        assert proc.stdout is not None
        while True:
            if time.monotonic() > deadline:
                _terminate(proc)
                timed_out = True
                break
            line = proc.stdout.readline()
            if not line:
                # EOF; child closed stdout. Wait briefly for exit so we
                # can capture stderr and exit code.
                break
            line = line.rstrip('\n')
            if not line.strip():
                continue
            if transcript_file is not None:
                transcript_file.write(line + '\n')
                transcript_file.flush()
            try:
                event_obj: object = json.loads(line)
            except json.JSONDecodeError:
                # Tolerate malformed lines — they're rare and shouldn't
                # crash the run. The transcript still records the raw text.
                continue
            if not isinstance(event_obj, dict):
                continue
            event = cast('dict[str, Any]', event_obj)
            snapshot.feed(event)
            if on_event is not None:
                with contextlib.suppress(Exception):
                    # Callbacks must never break the spawn loop.
                    on_event(event, snapshot)
    finally:
        if transcript_file is not None:
            transcript_file.close()

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _terminate(proc)
        proc.wait(timeout=2)
    stderr_thread.join(timeout=1)

    exit_code = proc.returncode if proc.returncode is not None else -1

    if timed_out:
        return ClaudeRunResult(
            is_error=True,
            exit_code=exit_code,
            result_text=f'claude timed out after {timeout_seconds}s',
            total_cost_usd=snapshot.total_cost_usd,
            usage=_extract_usage_map(snapshot.final_event)
            if snapshot.final_event is not None
            else {},
            raw=snapshot.final_event or {},
            timed_out=True,
            transcript_path=transcript_path,
        )

    if snapshot.final_event is None:
        stderr_text = ''.join(stderr_lines).strip()
        raise ClaudeRunError(
            f'claude produced no result event (exit {exit_code}). '
            f'stderr={stderr_text[:400]!r}'
        )

    return _build_result(snapshot, exit_code, transcript_path)


def _build_args(
    prompt: str,
    *,
    allowed_tools: list[str],
    mcp_config_path: Path | None,
    max_turns: int,
    cli: str,
    permission_mode: str | None = None,
    model: str | None = None,
) -> list[str]:
    args = [
        cli,
        '-p',
        prompt,
        '--output-format',
        'stream-json',
        '--verbose',
        '--max-turns',
        str(max_turns),
    ]
    if allowed_tools:
        args.extend(['--allowed-tools', ','.join(allowed_tools)])
    if mcp_config_path is not None:
        args.extend(['--mcp-config', str(mcp_config_path)])
    if permission_mode is not None:
        args.extend(['--permission-mode', permission_mode])
    if model is not None:
        args.extend(['--model', model])
    return args


# Tokens that must never reach the spawned `claude -p`: the bundled MCP
# proxies hold them in-process, and a leak into claude's env defeats the
# whole point of routing GitHub/Linear traffic through the proxies.
# Stripping unconditionally (even when no extra_env is passed) hardens the
# default path against a `claude -p` invocation that forgets to scrub.
_TOKEN_ENV_KEYS: frozenset[str] = frozenset(
    {
        'GITHUB_PERSONAL_ACCESS_TOKEN',
        'GH_TOKEN',
        'LINEAR_API_KEY',
    }
)
_TOKEN_ENV_PREFIXES: tuple[str, ...] = ('LINEAR_API_KEY_',)


def _is_token_env_key(key: str) -> bool:
    return key in _TOKEN_ENV_KEYS or any(key.startswith(p) for p in _TOKEN_ENV_PREFIXES)


def _merged_env(extra_env: Mapping[str, str] | None) -> Mapping[str, str]:
    """Return the env passed to the spawned `claude -p`.

    Always strips token env vars (GitHub PAT, `GH_TOKEN`, `LINEAR_API_KEY`
    and per-agent `LINEAR_API_KEY_*` variants) from the inherited
    `os.environ` so credential isolation is structural, not contingent on
    the caller. `extra_env` is layered on top — callers can still inject
    `ALBEDO_*` proxy plumbing without re-introducing tokens.
    """
    merged = {k: v for k, v in os.environ.items() if not _is_token_env_key(k)}
    if extra_env is not None:
        merged.update(extra_env)
    return merged


def _drain_stream(
    stream: object,
    sink: list[str],
) -> None:
    """Read everything from a text stream into a list. Used for stderr.

    `subprocess.PIPE` with text=True yields a TextIOWrapper; we type it
    loosely here because Popen's generic typing requires too much ceremony.
    """
    try:
        for chunk in stream:  # type: ignore[attr-defined,reportUnknownVariableType]
            sink.append(cast('str', chunk))
    except Exception:
        return


def _terminate(proc: subprocess.Popen[str]) -> None:
    """Kill the claude subprocess and its children.

    `start_new_session=True` makes the child a new process group leader,
    so we can SIGTERM the whole tree (any sub-agents claude spawned).
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()


def _resolve_transcript_path(
    transcript_dir: Path | None,
    basename: str | None,
) -> Path | None:
    if transcript_dir is None:
        return None
    name_part = basename or 'spawn'
    return transcript_dir / f'{name_part}-{int(time.time())}.jsonl'


def _build_result(
    snapshot: StreamSnapshot,
    exit_code: int,
    transcript_path: Path | None,
) -> ClaudeRunResult:
    final = snapshot.final_event or {}
    is_error = bool(final.get('is_error', exit_code != 0))
    return ClaudeRunResult(
        is_error=is_error,
        exit_code=exit_code,
        result_text=snapshot.final_result_text,
        total_cost_usd=snapshot.total_cost_usd,
        usage=_extract_usage_map(final),
        raw=final,
        transcript_path=transcript_path,
    )


def _extract_usage_map(final: Mapping[str, Any]) -> Mapping[str, int]:
    usage_raw = final.get('usage')
    out: dict[str, int] = {}
    if isinstance(usage_raw, dict):
        usage = cast('dict[str, Any]', usage_raw)
        for k, v in usage.items():
            if isinstance(v, int | float) and not isinstance(v, bool):
                out[str(k)] = int(v)
    return out
