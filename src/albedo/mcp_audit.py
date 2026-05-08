"""Append-only JSONL audit log for MCP proxy tool calls.

Shared sink for the upcoming GitHub and Linear MCP proxy children.
Every tool call appends one line to `<state_dir>/logs/mcp-audit.jsonl`
so two worker processes can write concurrently without coordinating:
the kernel guarantees atomic `O_APPEND` writes for payloads under
PIPE_BUF (typically 4096 bytes), and a single audit entry comfortably
fits inside that limit.

State-dir layout matches `logging_setup.configure_logging` — the audit
file lives next to `supervisor.log` and `agent-<id>.log`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

AUDIT_FILENAME = 'mcp-audit.jsonl'
AUDIT_LOG_DIRNAME = 'logs'
ARGS_SUMMARY_MAX_CHARS = 256

_logger = logging.getLogger(__name__)


def audit_log_path(state_dir: Path) -> Path:
    """Resolve the audit file path for `state_dir`.

    Matches `configure_logging`'s `<state_dir>/logs/...` layout.
    """
    return state_dir / AUDIT_LOG_DIRNAME / AUDIT_FILENAME


def summarize_args(args: Any) -> str:
    """Produce a JSON-shaped, length-capped preview of `args`.

    Callers are responsible for redacting secrets before passing args
    in — this only enforces the 256-char cap so a verbose payload
    cannot blow up the audit file.
    """
    if isinstance(args, str):
        text = args
    else:
        try:
            text = json.dumps(args, default=str, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(args)
    return _truncate(text)


def _truncate(text: str) -> str:
    if len(text) <= ARGS_SUMMARY_MAX_CHARS:
        return text
    return text[: ARGS_SUMMARY_MAX_CHARS - 1] + '…'


def record_audit(
    *,
    state_dir: Path,
    issue_id: str,
    agent_id: str,
    tool: str,
    args_summary: str,
    outcome: str,
    latency_ms: int,
) -> None:
    """Append one audit entry to `<state_dir>/logs/mcp-audit.jsonl`.

    The entry is a JSON object containing `ts`, `issue_id`, `agent_id`,
    `tool`, `args_summary`, `outcome`, `latency_ms`. `args_summary` is
    truncated to 256 chars defensively; `latency_ms` is coerced to a
    non-negative int.

    If the log directory cannot be created or the file cannot be
    opened for append, the failure is swallowed and re-emitted at
    WARNING level — proxies must never fail a tool call solely
    because audit write failed.
    """
    entry = {
        'ts': datetime.now(UTC).isoformat(),
        'issue_id': issue_id,
        'agent_id': agent_id,
        'tool': tool,
        'args_summary': _truncate(args_summary),
        'outcome': outcome,
        'latency_ms': max(0, int(latency_ms)),
    }
    line = json.dumps(entry, ensure_ascii=False) + '\n'

    log_dir = state_dir / AUDIT_LOG_DIRNAME
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / AUDIT_FILENAME).open('a', encoding='utf-8') as fh:
            fh.write(line)
    except OSError as exc:
        _logger.warning('mcp-audit write failed: %s', exc)
