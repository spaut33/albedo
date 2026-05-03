"""Pure utilities for the CI-failure redispatch flow.

Mirrors `comment_redispatch.py` for CI runs: extract the Linear
identifier from a branch name, truncate the failing-step log tail,
format the comment body posted back on the issue, and round-trip a
per-issue dedup state file mapping `issue_id -> last seen completed
run id`.

No Linear or GitHub orchestration lives here — those land in the next
child of AI-56. This module is import-pure: only `re`, `json`,
`logging`, and `pathlib`.

Loop avoidance: per-issue `last_ci_run.json` records the most recently
observed completed run id. A new run id fires once, then the file pins
it so the next tick skips. The file is updated on first observation
without firing — runs that pre-date the feature roll-out don't trigger
spurious comments.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, cast

log = logging.getLogger(__name__)

LAST_CI_RUN_STATE_FILE = 'last_ci_run.json'
LAST_CI_RUN_STATE_VERSION = 1

DEFAULT_LOG_TAIL_LINES = 200

# Case-insensitive Linear identifier (e.g. AI-58, eng-12). `\b` anchors
# keep us off `notai-58`-style false positives. First match wins, so a
# branch like `task/AI-58-fix-AI-12` resolves to AI-58.
_IDENTIFIER_RE = re.compile(r'\b([a-z]+-\d+)\b', re.IGNORECASE)


def extract_linear_identifier(branch: str) -> str | None:
    """Return the first Linear identifier in `branch`, upper-cased, else None.

    Independent of any `task/` prefix — bare branches like `AI-58` and
    nested branches like `feature/AI-58-foo` both resolve.
    """
    match = _IDENTIFIER_RE.search(branch)
    if match is None:
        return None
    return match.group(1).upper()


def truncate_log_tail(log: str, max_lines: int = DEFAULT_LOG_TAIL_LINES) -> str:
    """Return at most the last `max_lines` lines of `log`.

    When the input has more than `max_lines` lines, prepend a single
    elision marker line: `… (N earlier lines elided)` where N is the
    number of dropped lines. Otherwise the input is returned unchanged.
    """
    if not log:
        return log
    lines = log.splitlines()
    if len(lines) <= max_lines:
        return log
    dropped = len(lines) - max_lines
    kept = lines[-max_lines:]
    marker = f'… ({dropped} earlier lines elided)'
    return '\n'.join([marker, *kept])


def format_ci_failure_comment(
    *,
    workflow_name: str,
    run_url: str,
    job_name: str,
    step_name: str,
    log_tail: str,
) -> str:
    """Render the CI-failure comment body posted back to Linear.

    Shape: header line, `Failed job:` line, fenced ```log block. The
    log block is wrapped verbatim — callers should pass an already
    truncated tail via `truncate_log_tail`.
    """
    header = f'**CI failure**: workflow `{workflow_name}` — [run]({run_url})'
    failed = f'Failed job: `{job_name}` / step `{step_name}`'
    fence = f'```log\n{log_tail}\n```'
    return f'{header}\n{failed}\n{fence}'


def load_last_ci_run(state_dir: Path) -> dict[str, str]:
    """Read the `{issue_id: run_id}` map, tolerating absence and corruption.

    Missing file → empty dict. Corrupt JSON, wrong shape, or wrong
    version → empty dict + logged warning. Mirrors
    `comment_redispatch._load_seen` so both feeds fail soft on a single
    poll cycle without raising into the supervisor.
    """
    path = _state_path(state_dir)
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning('ci_redispatch state read failed at %s: %s', path, exc)
        return {}
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning('ci_redispatch state at %s is corrupt: %s', path, exc)
        return {}
    if not isinstance(parsed, dict):
        log.warning('ci_redispatch state at %s has unexpected shape', path)
        return {}
    data = cast(dict[str, Any], parsed)
    if data.get('version') != LAST_CI_RUN_STATE_VERSION:
        return {}
    runs_raw = data.get('runs', {})
    if not isinstance(runs_raw, dict):
        return {}
    runs_typed = cast(dict[str, Any], runs_raw)
    return {str(k): str(v) for k, v in runs_typed.items()}


def save_last_ci_run(state_dir: Path, mapping: dict[str, str]) -> None:
    """Atomically persist the `{issue_id: run_id}` map.

    Writes through a sibling `.tmp` file then renames so a crashed write
    cannot leave a half-truncated state file behind. Repeated saves of
    the same mapping produce identical bytes (idempotent).
    """
    path = _state_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'version': LAST_CI_RUN_STATE_VERSION,
        'runs': dict(sorted(mapping.items())),
    }
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload), encoding='utf-8')
    tmp.replace(path)


def seed_first_observation(state: dict[str, str], issue_id: str, run_id: str) -> bool:
    """Record `run_id` for `issue_id` only on first observation.

    Returns True when this call seeded a fresh entry (caller should NOT
    fire a comment), False when `issue_id` already had a prior run id
    (caller decides whether the new id warrants firing). Mutates `state`
    in place when seeding. Mirrors `comment_redispatch`'s seed-without-
    fire so historical runs don't replay after rollout.
    """
    if issue_id in state:
        return False
    state[issue_id] = run_id
    return True


def _state_path(state_dir: Path) -> Path:
    return state_dir / LAST_CI_RUN_STATE_FILE
