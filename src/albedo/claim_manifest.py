"""Per-agent claim manifest persisted alongside the heartbeat.

When a worker successfully claims an issue and moves it to `In Progress`,
it writes a small JSON file recording the issue id and the state it
came from. On the success path the worker clears the manifest after the
post-spawn handler moves the issue to its target column. On a crash or
abrupt shutdown, stale-claim recovery reads the manifest to restore the
issue back to its original state before un-assigning it — without this
the issue would be stranded in `In Progress` (not in any pickup set).

Lives next to the heartbeat so it shares state-dir conventions and
cleanup. One manifest per agent: an agent only ever holds one issue at
a time.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

log = logging.getLogger(__name__)

CLAIM_MANIFEST_VERSION = 1


@dataclass(frozen=True, slots=True)
class ClaimManifest:
    issue_id: str
    issue_identifier: str
    prev_state_id: str
    prev_state_name: str
    claimed_at_unix: float


def claim_manifest_path(state_dir: Path, agent_id: str) -> Path:
    return state_dir / f'agent-{agent_id}.claim.json'


def write_claim_manifest(path: Path, manifest: ClaimManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'version': CLAIM_MANIFEST_VERSION,
        'issue_id': manifest.issue_id,
        'issue_identifier': manifest.issue_identifier,
        'prev_state_id': manifest.prev_state_id,
        'prev_state_name': manifest.prev_state_name,
        'claimed_at_unix': manifest.claimed_at_unix,
    }
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(payload), encoding='utf-8')
    tmp.replace(path)


def read_claim_manifest(path: Path) -> ClaimManifest | None:
    """Return the manifest at `path`, or `None` if missing or unreadable.

    A corrupt or schema-mismatched file is treated as missing so recovery
    can still proceed (un-assign without state restoration is preferable
    to crashing the recovery loop).
    """
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning('claim manifest read failed at %s: %s', path, exc)
        return None
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning('claim manifest at %s is corrupt: %s', path, exc)
        return None
    if not isinstance(parsed, dict):
        log.warning('claim manifest at %s has unexpected schema', path)
        return None
    data = cast(dict[str, Any], parsed)
    if data.get('version') != CLAIM_MANIFEST_VERSION:
        log.warning('claim manifest at %s has unexpected schema', path)
        return None
    try:
        return ClaimManifest(
            issue_id=str(data['issue_id']),
            issue_identifier=str(data['issue_identifier']),
            prev_state_id=str(data['prev_state_id']),
            prev_state_name=str(data['prev_state_name']),
            claimed_at_unix=float(data['claimed_at_unix']),
        )
    except (KeyError, TypeError, ValueError) as exc:
        log.warning('claim manifest at %s is missing fields: %s', path, exc)
        return None


def clear_claim_manifest(path: Path) -> None:
    """Remove the manifest. No-op if absent."""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning('claim manifest delete failed at %s: %s', path, exc)
