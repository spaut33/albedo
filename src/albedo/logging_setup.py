"""Structured logging via structlog with stdlib bridging.

Two outputs by default:
  - stderr: human-readable (`ConsoleRenderer`), suitable for `tail -f`
    or `journalctl`. No colors so logs stay readable when piped.
  - file: JSON, one event per line, rotating. Lives at
    `<state_dir>/logs/<process>.log`. `<process>` is `supervisor` for
    the parent and `agent-<id>` for each worker child.

Existing `logging.getLogger(__name__).info(...)` call sites continue to
work — the stdlib root logger is wired into structlog's
`ProcessorFormatter` so foreign records get the same shape as native
structlog events. New code should prefer `structlog.get_logger()` plus
`bind_contextvars(...)` so that fields like `agent` and `issue` appear
in every downstream log line without manual plumbing.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars
from structlog.stdlib import ProcessorFormatter
from structlog.types import Processor

DEFAULT_LOG_DIRNAME = 'logs'
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt='iso', utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]


def configure_logging(
    *,
    agent_id: str | None,
    level: str,
    state_dir: Path,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    silence_stderr: bool = False,
) -> Path:
    """Configure structlog + stdlib logging for this process.

    Returns the path of the per-process log file so callers can surface
    it (e.g. for `--once` debugging output). Re-running is safe: existing
    handlers on the root logger are removed first so a worker child can
    re-configure after `multiprocessing` spawn.
    """
    log_dir = state_dir / DEFAULT_LOG_DIRNAME
    log_dir.mkdir(parents=True, exist_ok=True)
    filename = f'agent-{agent_id}.log' if agent_id else 'supervisor.log'
    file_path = log_dir / filename

    shared = _shared_processors()
    structlog.configure(
        processors=[*shared, ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    json_formatter = ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    console_formatter = ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
    )

    file_handler = RotatingFileHandler(
        file_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8',
    )
    file_handler.setFormatter(json_formatter)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(console_formatter)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(file_handler)
    if not silence_stderr:
        root.addHandler(stderr_handler)
    root.setLevel(getattr(logging, level.upper()))

    # Quiet noisy third-party loggers — every Linear poll otherwise emits
    # an INFO line like `HTTP Request: POST https://api.linear.app/...`,
    # which crowds the events panel and the per-agent log file.
    for noisy in ('httpx', 'httpcore', 'urllib3'):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    clear_contextvars()
    if agent_id is not None:
        bind_contextvars(agent=agent_id)

    return file_path
