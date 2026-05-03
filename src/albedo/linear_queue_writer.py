"""Per-supervisor Linear queue snapshot file.

Each worker republishes the pickup-state counts to `state/linear_queue.json`
right after a successful poll. Workers race; last writer wins, which is
fine for the TUI (it just wants a recent number, not a transactional one).

Schema:

    {
      "counts": {"Triage": 0, "Backlog": 2, "Review": 1, ...},
      "updated_at": 1714654329.5,
      "agent_id": "1"
    }

The TUI reads `state/linear_queue.json` via `tui.snapshot.load_linear_queue`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from albedo.linear_client import Issue

log = logging.getLogger(__name__)

QUEUE_FILE = 'linear_queue.json'


def queue_path(state_dir: Path) -> Path:
    return state_dir / QUEUE_FILE


def publish_queue(
    state_dir: Path,
    *,
    agent_id: str,
    issues: Iterable[Issue],
    pickup_states: Iterable[str] = (),
) -> None:
    """Write a snapshot of pickup-state counts atomically.

    `pickup_states` is included as zero-rows so the TUI shows columns
    that happen to be empty (otherwise the panel would silently drop
    "Triage 0" rows when the team is idle there).
    """
    counts: Counter[str] = Counter()
    for state in pickup_states:
        counts[state] = 0
    for issue in issues:
        counts[issue.state_name] += 1
    path = queue_path(state_dir)
    tmp = path.with_suffix('.json.tmp')
    payload = {
        'counts': dict(counts),
        'updated_at': time.time(),
        'agent_id': agent_id,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, path)
    except OSError as exc:
        log.warning('linear queue publish failed at %s: %s', path, exc)
