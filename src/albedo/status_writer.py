"""Per-worker status snapshot file used by the live TUI.

Each worker writes `state/agent-<id>.status.json` atomically on every phase
change and during a claude spawn (driven by the stream-event callback).
The TUI polls these files at 4 Hz and renders the aggregate. Layout
mirrors the same atomic-write pattern used by `claim_manifest.py`:
write to a `.tmp` sibling, then `replace` onto the target.

The schema is intentionally flat — no nested versioning. Old fields stay
present for forward compatibility; new ones are added with defaults.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from albedo.stream_parser import StreamSnapshot, ToolCallSummary

log = logging.getLogger(__name__)

# Phase constants. Kept here (not as enum) so the JSON values remain stable
# strings without string conversion ceremony at the edges.
PHASE_BOOTING = 'booting'
PHASE_POLLING = 'polling'
PHASE_THROTTLED = 'throttled'
PHASE_CLAIMING = 'claiming'
PHASE_IN_PROGRESS = 'in_progress_transition'
PHASE_SPAWNING_CLAUDE = 'spawning_claude'
PHASE_RUNNING_CLAUDE = 'running_claude'
PHASE_POST_SPAWN = 'post_spawn_linear'
PHASE_CLEANUP = 'cleanup'
PHASE_SHUTDOWN = 'shutdown'

ALL_PHASES: tuple[str, ...] = (
    PHASE_BOOTING,
    PHASE_POLLING,
    PHASE_THROTTLED,
    PHASE_CLAIMING,
    PHASE_IN_PROGRESS,
    PHASE_SPAWNING_CLAUDE,
    PHASE_RUNNING_CLAUDE,
    PHASE_POST_SPAWN,
    PHASE_CLEANUP,
    PHASE_SHUTDOWN,
)


def _empty_recent_tools() -> list[dict[str, Any]]:
    return []


@dataclass(slots=True)
class IssueRef:
    id: str
    identifier: str
    title: str
    url: str = ''


@dataclass(slots=True)
class StreamSummary:
    turns: int = 0
    tool_use_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    last_tool_name: str = ''
    last_tool_target: str = ''
    last_tool_ts: float = 0.0
    thinking_preview: str = ''
    recent_tools: list[dict[str, Any]] = field(default_factory=_empty_recent_tools)


@dataclass(slots=True)
class WorkerStatus:
    agent_id: str
    updated_at: float
    phase: str
    phase_started_at: float
    issue: IssueRef | None = None
    role: str = ''
    worktree: str = ''
    branch: str = ''
    stream: StreamSummary = field(default_factory=StreamSummary)
    last_event: str = ''
    display_name: str = ''


def status_path(state_dir: Path, agent_id: str) -> Path:
    return state_dir / f'agent-{agent_id}.status.json'


def _summary_from_snapshot(snapshot: StreamSnapshot) -> StreamSummary:
    last = snapshot.last_tool
    summary = StreamSummary(
        turns=snapshot.turns,
        tool_use_count=snapshot.tool_use_count,
        input_tokens=snapshot.input_tokens,
        output_tokens=snapshot.output_tokens,
        cache_read_tokens=snapshot.cache_read_tokens,
        cache_creation_tokens=snapshot.cache_creation_tokens,
        thinking_preview=snapshot.thinking_preview,
        recent_tools=[_tool_to_dict(t) for t in snapshot.recent_tools],
    )
    if last is not None:
        summary.last_tool_name = last.name
        summary.last_tool_target = last.target
        summary.last_tool_ts = last.ts
    return summary


def _tool_to_dict(tool: ToolCallSummary) -> dict[str, Any]:
    return {'name': tool.name, 'target': tool.target, 'ts': tool.ts}


class StatusWriter:
    """Atomic status writer for one worker.

    Methods are intentionally side-effecting (write the file on every call)
    so the TUI sees the latest snapshot within ~250 ms of any state change.
    Failures are logged at WARNING and swallowed — losing observability
    must never block a worker from making real progress.
    """

    def __init__(
        self, *, state_dir: Path, agent_id: str, display_name: str = ''
    ) -> None:
        self._path = status_path(state_dir, agent_id)
        self._tmp = self._path.with_suffix('.json.tmp')
        self._status = WorkerStatus(
            agent_id=agent_id,
            updated_at=time.time(),
            phase=PHASE_BOOTING,
            phase_started_at=time.time(),
            display_name=display_name,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.flush()

    def set_display_name(self, name: str) -> None:
        self._status.display_name = name
        self.flush()

    # --- mutating API ------------------------------------------------------

    def set_phase(
        self, phase: str, *, last_event: str = '', reset_clock: bool = True
    ) -> None:
        """Update the phase string and (optionally) the elapsed clock.

        `reset_clock=False` keeps `phase_started_at` untouched — useful for
        sub-phase transitions like spawning_claude → running_claude where
        the user wants a continuous "time on this task" counter rather than
        each sub-phase restarting from zero.
        """
        self._status.phase = phase
        if reset_clock:
            self._status.phase_started_at = time.time()
        if last_event:
            self._status.last_event = last_event
        self.flush()

    def set_issue(
        self,
        *,
        issue_id: str,
        identifier: str,
        title: str,
        role: str,
        worktree: str = '',
        branch: str = '',
        url: str = '',
    ) -> None:
        self._status.issue = IssueRef(
            id=issue_id, identifier=identifier, title=title, url=url
        )
        self._status.role = role
        self._status.worktree = worktree
        self._status.branch = branch
        self._status.stream = StreamSummary()
        self.flush()

    def clear_issue(self) -> None:
        self._status.issue = None
        self._status.role = ''
        self._status.worktree = ''
        self._status.branch = ''
        self._status.stream = StreamSummary()
        self.flush()

    def update_stream(self, snapshot: StreamSnapshot) -> None:
        self._status.stream = _summary_from_snapshot(snapshot)
        self.flush()

    def note(self, message: str) -> None:
        """Record a one-line free-form event (last_event field)."""
        self._status.last_event = message
        self.flush()

    # --- I/O ---------------------------------------------------------------

    def flush(self) -> None:
        self._status.updated_at = time.time()
        try:
            payload = _to_json(self._status)
            self._tmp.write_text(payload, encoding='utf-8')
            os.replace(self._tmp, self._path)
        except OSError as exc:
            log.warning('status writer flush failed at %s: %s', self._path, exc)

    def remove(self) -> None:
        """Delete the status file. Used on graceful shutdown."""
        for path in (self._tmp, self._path):
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                log.warning('status writer remove failed at %s: %s', path, exc)


def _to_json(status: WorkerStatus) -> str:
    payload: dict[str, Any] = {
        'agent_id': status.agent_id,
        'display_name': status.display_name,
        'updated_at': status.updated_at,
        'phase': status.phase,
        'phase_started_at': status.phase_started_at,
        'role': status.role,
        'worktree': status.worktree,
        'branch': status.branch,
        'last_event': status.last_event,
        'stream': asdict(status.stream),
    }
    if status.issue is not None:
        payload['issue'] = asdict(status.issue)
    else:
        payload['issue'] = None
    return json.dumps(payload, ensure_ascii=False)


def read_status(path: Path) -> WorkerStatus | None:
    """Reader-side helper used by the TUI snapshot aggregator.

    Returns `None` when the file is missing or malformed. Partial JSON is
    impossible because writes go through `os.replace`.
    """
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning('status read failed at %s: %s', path, exc)
        return None
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning('status at %s is malformed: %s', path, exc)
        return None
    if not isinstance(parsed, dict):
        return None
    data = cast('dict[str, Any]', parsed)
    issue_obj = data.get('issue')
    issue: IssueRef | None = None
    if isinstance(issue_obj, dict):
        issue_dict = cast('dict[str, Any]', issue_obj)
        try:
            issue = IssueRef(
                id=str(issue_dict['id']),
                identifier=str(issue_dict['identifier']),
                title=str(issue_dict.get('title', '')),
                url=str(issue_dict.get('url', '')),
            )
        except KeyError:
            issue = None
    stream_obj = data.get('stream')
    if isinstance(stream_obj, dict):
        stream_dict = cast('dict[str, Any]', stream_obj)
    else:
        stream_dict = {}
    stream = _stream_from_dict(stream_dict)
    return WorkerStatus(
        agent_id=str(data.get('agent_id', '')),
        updated_at=float(data.get('updated_at', 0.0)),
        phase=str(data.get('phase', PHASE_BOOTING)),
        phase_started_at=float(data.get('phase_started_at', 0.0)),
        issue=issue,
        role=str(data.get('role', '')),
        worktree=str(data.get('worktree', '')),
        branch=str(data.get('branch', '')),
        stream=stream,
        last_event=str(data.get('last_event', '')),
        display_name=str(data.get('display_name', '')),
    )


def _stream_from_dict(data: dict[str, Any]) -> StreamSummary:
    recent_raw = data.get('recent_tools')
    recent: list[dict[str, Any]] = []
    if isinstance(recent_raw, list):
        recent_list = cast('list[Any]', recent_raw)
        for item in recent_list:
            if isinstance(item, dict):
                item_dict = cast('dict[str, Any]', item)
                recent.append(
                    {
                        'name': str(item_dict.get('name', '')),
                        'target': str(item_dict.get('target', '')),
                        'ts': float(item_dict.get('ts', 0.0)),
                    }
                )
    return StreamSummary(
        turns=int(data.get('turns', 0) or 0),
        tool_use_count=int(data.get('tool_use_count', 0) or 0),
        input_tokens=int(data.get('input_tokens', 0) or 0),
        output_tokens=int(data.get('output_tokens', 0) or 0),
        cache_read_tokens=int(data.get('cache_read_tokens', 0) or 0),
        cache_creation_tokens=int(data.get('cache_creation_tokens', 0) or 0),
        last_tool_name=str(data.get('last_tool_name', '')),
        last_tool_target=str(data.get('last_tool_target', '')),
        last_tool_ts=float(data.get('last_tool_ts', 0.0)),
        thinking_preview=str(data.get('thinking_preview', '')),
        recent_tools=recent,
    )
