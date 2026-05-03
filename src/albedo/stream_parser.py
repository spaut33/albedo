"""Parse `claude -p --output-format stream-json --verbose` events.

The stream emits one JSON object per line. We care about a small subset:

* `assistant` events contain a `message.content` array. Items of type
  `tool_use` (`{"type":"tool_use","name":...,"input":{...}}`) and `text`
  (`{"type":"text","text":...}`) feed the live snapshot. The `message.usage`
  block tracks running token counts.
* `result` event (`{"type":"result", ...}`) is the terminal summary —
  same shape as `--output-format json`.
* Everything else (system init, hook, rate_limit_event, user/tool_result)
  is ignored for the live view but persisted to the transcript file.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast

THINKING_PREVIEW_CHARS = 80
RECENT_TOOL_BUFFER = 5
TARGET_PREVIEW_CHARS = 60


@dataclass(slots=True)
class ToolCallSummary:
    name: str
    target: str
    ts: float


@dataclass(slots=True)
class StreamSnapshot:
    """Mutable running summary fed by `feed()`.

    Workers pass this (or a copy) to the StatusWriter on every event so the
    TUI can render fresh per-agent activity. Frozen=False intentionally —
    we mutate in place to avoid per-event allocation churn.
    """

    turns: int = 0
    tool_use_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    last_tool: ToolCallSummary | None = None
    thinking_preview: str = ''
    recent_tools: deque[ToolCallSummary] = field(
        default_factory=lambda: deque(maxlen=RECENT_TOOL_BUFFER)
    )
    finished: bool = False
    is_error: bool = False
    total_cost_usd: float = 0.0
    final_result_text: str = ''
    final_event: Mapping[str, Any] | None = None

    def feed(self, event: Mapping[str, Any]) -> None:
        etype = event.get('type')
        if etype == 'assistant':
            self._handle_assistant(event)
        elif etype == 'result':
            self._handle_result(event)

    def _handle_assistant(self, event: Mapping[str, Any]) -> None:
        self.turns += 1
        message_obj = event.get('message')
        if not isinstance(message_obj, dict):
            return
        message = cast('dict[str, Any]', message_obj)
        usage_obj = message.get('usage')
        if isinstance(usage_obj, dict):
            usage = cast('dict[str, Any]', usage_obj)
            self.input_tokens = _coerce_int(
                usage.get('input_tokens'), self.input_tokens
            )
            self.output_tokens = _coerce_int(
                usage.get('output_tokens'), self.output_tokens
            )
            self.cache_read_tokens = _coerce_int(
                usage.get('cache_read_input_tokens'), self.cache_read_tokens
            )
            self.cache_creation_tokens = _coerce_int(
                usage.get('cache_creation_input_tokens'), self.cache_creation_tokens
            )
        content_obj = message.get('content')
        if not isinstance(content_obj, list):
            return
        content = cast('list[Any]', content_obj)
        now = time.time()
        for block_obj in content:
            if not isinstance(block_obj, dict):
                continue
            block = cast('dict[str, Any]', block_obj)
            btype = block.get('type')
            if btype == 'tool_use':
                self._record_tool_use(block, now)
            elif btype == 'text':
                self._record_text(block)

    def _record_tool_use(self, block: Mapping[str, Any], ts: float) -> None:
        self.tool_use_count += 1
        name = str(block.get('name') or 'tool')
        target = _summarize_tool_input(name, block.get('input'))
        summary = ToolCallSummary(name=name, target=target, ts=ts)
        self.last_tool = summary
        self.recent_tools.append(summary)

    def _record_text(self, block: Mapping[str, Any]) -> None:
        text_raw = block.get('text')
        if not isinstance(text_raw, str):
            return
        cleaned = text_raw.strip().replace('\n', ' ')
        if not cleaned:
            return
        self.thinking_preview = cleaned[:THINKING_PREVIEW_CHARS]

    def _handle_result(self, event: Mapping[str, Any]) -> None:
        self.finished = True
        self.is_error = bool(event.get('is_error', False))
        cost = event.get('total_cost_usd', 0.0)
        if isinstance(cost, int | float) and not isinstance(cost, bool):
            self.total_cost_usd = float(cost)
        result = event.get('result', '')
        self.final_result_text = result if isinstance(result, str) else ''
        self.final_event = event
        usage_obj = event.get('usage')
        if isinstance(usage_obj, dict):
            usage = cast('dict[str, Any]', usage_obj)
            self.input_tokens = _coerce_int(
                usage.get('input_tokens'), self.input_tokens
            )
            self.output_tokens = _coerce_int(
                usage.get('output_tokens'), self.output_tokens
            )
            self.cache_read_tokens = _coerce_int(
                usage.get('cache_read_input_tokens'), self.cache_read_tokens
            )
            self.cache_creation_tokens = _coerce_int(
                usage.get('cache_creation_input_tokens'), self.cache_creation_tokens
            )


def _coerce_int(value: object, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int | float):
        return int(value)
    return fallback


_FILE_PATH_KEYS: tuple[str, ...] = ('file_path', 'path', 'notebook_path')


def _summarize_tool_input(name: str, raw: object) -> str:
    """Best-effort one-line description of what a tool call is doing."""
    if not isinstance(raw, dict):
        return ''
    data = cast('dict[str, Any]', raw)
    lower = name.lower()
    if lower == 'bash':
        cmd = data.get('command')
        if isinstance(cmd, str):
            return _truncate(cmd.replace('\n', ' '))
    if lower in {'grep', 'glob'}:
        pattern = data.get('pattern') or data.get('path')
        if isinstance(pattern, str):
            return _truncate(pattern)
    if lower == 'task':
        desc = data.get('description') or data.get('subagent_type')
        if isinstance(desc, str):
            return _truncate(desc)
    if lower.startswith('mcp__'):
        # MCP tool — most carry small structured payloads. Show keys rather
        # than dumping potentially large arg blobs.
        keys = sorted(k for k in data if not k.startswith('_'))
        return _truncate(', '.join(keys[:4]))
    for key in _FILE_PATH_KEYS:
        value = data.get(key)
        if isinstance(value, str):
            return _truncate(value)
    # Fall back to the first short string field, if any.
    for value in data.values():
        if isinstance(value, str) and len(value) <= TARGET_PREVIEW_CHARS:
            return value
    return ''


def _truncate(value: str) -> str:
    if len(value) <= TARGET_PREVIEW_CHARS:
        return value
    return value[: TARGET_PREVIEW_CHARS - 1] + '…'
