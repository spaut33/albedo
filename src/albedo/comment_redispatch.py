"""Re-trigger work on issues parked in human-gated states.

Two distinct cases land in human-gated parking, and both need a way back
out when a user comment arrives:

A. `Awaiting approval` — no role in the dispatch table picks it up, so
   the issue sits forever unless a human moves it. Branch by label:

     - `kind:decomposition` (architect output, awaiting human OK) →
       archive children and move the parent back to `Triage`.
     - `stuck` (reviewer escalated after MAX_ATTEMPTS) → strip `stuck`
       and move to `Backlog`.
     - `kind:final-pr` (coder PR awaiting human merge) → move back to
       `Review`.

B. `Triage` / `Backlog` with `awaiting-human-reply` (architect or coder
   asked a clarifying question via `BLOCKED:`) — that label sits in
   `worker.EXCLUDE_LABELS`, so the worker pool stops re-picking the
   issue until the label is stripped. On a new user comment we strip it
   in place; the issue is unassigned and stays in the same column,
   ready for the next poll.

Plain `Triage` (no parent, no label) and plain `Backlog` issues are
naturally re-picked by workers on the next poll, with the new comment
included in the prompt via Part A — no redispatch action needed.

Loop avoidance: per-issue `last_user_comment.json` records the most
recently observed user-authored comment id. A new comment fires once,
then the file pins it so the next tick skips. The file is updated on
first observation without firing — comments that pre-date the feature
roll-out don't trigger spurious re-dispatches.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from albedo.comment_filter import filter_user_comments, is_bot_comment
from albedo.linear_client import (
    ISSUE_FRAGMENT,
    Comment,
    Issue,
    IssueUpdate,
    LinearClient,
    issue_from_node,
)

# Triage callback signature: (agent_message, user_replies) -> is_substantive
# Returning False means the user's reply is too thin to act on; redispatch
# will pin `seen` to the latest comment id and skip the fire.
TriageFn = Callable[[str, Sequence[str]], bool]

log = logging.getLogger(__name__)

LAST_COMMENT_STATE_FILE = 'last_user_comment.json'
LAST_COMMENT_STATE_VERSION = 1

# Labels stripped on every redispatch — without this, pickup keeps skipping
# the issue (`worker.EXCLUDE_LABELS`).
_PICKUP_BLOCKERS: frozenset[str] = frozenset(
    {'stuck', 'blocked-external', 'awaiting-human-reply'}
)

# Labels that gate the Awaiting-approval branch.
_DECOMPOSITION_LABEL = 'kind:decomposition'
_STUCK_LABEL = 'stuck'
_FINAL_PR_LABEL = 'kind:final-pr'

# Label set on Triage/Backlog by an architect/coder BLOCKED question.
_AWAIT_HUMAN_LABEL = 'awaiting-human-reply'

# Sentinel `to_state` indicating the issue stays where it is — the
# redispatch only stripped the gating label. Workers re-pick it on the
# next poll.
_STAY = '<stay>'


@dataclass(frozen=True, slots=True)
class RedispatchResult:
    issue_identifier: str
    from_state: str
    to_state: str
    trigger_label: str


@dataclass(frozen=True, slots=True)
class RedispatchReport:
    inspected: int = 0
    fired: tuple[RedispatchResult, ...] = field(default_factory=tuple)
    seeded: tuple[str, ...] = field(default_factory=tuple)
    skipped_non_substantive: tuple[str, ...] = field(default_factory=tuple)


def redispatch_on_new_user_comments(
    *,
    linear: LinearClient,
    team_id: str,
    bot_user_ids: frozenset[str],
    state_dir: Path,
    project_id: str | None = None,
    triage_fn: TriageFn | None = None,
) -> RedispatchReport:
    """Re-route human-gated issues when a new user comment lands.

    Covers `Awaiting approval` (label-branch routing) and Triage/Backlog
    issues gated by `awaiting-human-reply` (label-strip in place).
    Idempotent across ticks via `last_user_comment.json`. `project_id`
    scopes redispatch to a single Linear project so the active profile
    never touches another's parked issues.

    If `triage_fn` is supplied, it is called with the agent's most recent
    message and the user replies that came after it. A `False` return
    pins `seen` to the latest comment id and suppresses the fire — the
    next human comment will trigger a fresh evaluation. None disables the
    gate (legacy behavior: any new comment fires).
    """
    state_path = state_dir / LAST_COMMENT_STATE_FILE
    seen = _load_seen(state_path)

    candidates = _list_human_gated_unassigned(linear, team_id, project_id)
    states_by_name = _states_for_team(linear, team_id) if candidates else {}

    fired: list[RedispatchResult] = []
    seeded: list[str] = []
    skipped: list[str] = []
    inspected = 0

    for issue in candidates:
        inspected += 1
        try:
            raw_comments = linear.list_comments(issue.id)
        except Exception as exc:
            log.warning(
                'redispatch: list_comments for %s failed: %s', issue.identifier, exc
            )
            continue
        user_comments = filter_user_comments(raw_comments, bot_user_ids)
        if not user_comments:
            continue
        latest_id = _latest_id(user_comments)
        prior_id = seen.get(issue.id)
        if prior_id == latest_id:
            continue
        if prior_id is None and issue.state_name == 'Awaiting approval':
            # Awaiting approval: seed-without-fire on the first
            # observation so historical comments don't replay after
            # rollout (could destructively re-architect an approved
            # decomposition or kick a `kind:final-pr` issue back to
            # Review on stale feedback).
            #
            # The `awaiting-human-reply` gate (Triage/Backlog) is fresh
            # by construction — the bot just set the label — so we fire
            # on first observation there. No rollout risk because the
            # gate didn't exist before this feature shipped.
            seen[issue.id] = latest_id
            seeded.append(issue.identifier)
            continue
        if triage_fn is not None and not _passes_triage(
            issue=issue,
            raw_comments=raw_comments,
            bot_user_ids=bot_user_ids,
            triage_fn=triage_fn,
        ):
            seen[issue.id] = latest_id
            skipped.append(issue.identifier)
            continue
        result = _try_redispatch(
            linear=linear,
            issue=issue,
            states_by_name=states_by_name,
        )
        if result is not None:
            seen[issue.id] = latest_id
            fired.append(result)

    _save_seen(state_path, seen)
    log.info(
        'redispatch tick: inspected=%d fired=%d seeded=%d skipped=%d',
        inspected,
        len(fired),
        len(seeded),
        len(skipped),
    )
    return RedispatchReport(
        inspected=inspected,
        fired=tuple(fired),
        seeded=tuple(seeded),
        skipped_non_substantive=tuple(skipped),
    )


def _passes_triage(
    *,
    issue: Issue,
    raw_comments: Sequence[Comment],
    bot_user_ids: frozenset[str],
    triage_fn: TriageFn,
) -> bool:
    """Ask the triage callback whether the user reply warrants firing.

    The agent's most recent message anchors what's being replied to. User
    comments after that anchor are passed in chronological order. If
    there's no agent comment to anchor on (unexpected — gates are set by
    bot comments by construction), we fail-open and let redispatch fire.
    """
    agent_message, user_replies = _split_thread_at_last_agent_message(
        raw_comments, bot_user_ids
    )
    if agent_message is None:
        log.info(
            'triage: %s has no anchoring agent message — firing without triage',
            issue.identifier,
        )
        return True
    if not user_replies:
        log.info(
            'triage: %s has no user replies after the last agent message — '
            'firing without triage',
            issue.identifier,
        )
        return True
    try:
        verdict = triage_fn(agent_message, user_replies)
    except Exception as exc:
        log.warning(
            'triage: callback raised for %s (%s) — fail-open, firing',
            issue.identifier,
            exc,
        )
        return True
    if not verdict:
        log.info(
            'triage: %s reply judged non-substantive — leaving parked',
            issue.identifier,
        )
    return verdict


def _split_thread_at_last_agent_message(
    comments: Sequence[Comment],
    bot_user_ids: frozenset[str],
) -> tuple[str | None, list[str]]:
    """Return (last agent body, user-comment bodies posted after it).

    Walks the chronological list backward to find the most recent bot
    comment, then collects user comments that follow it. Empty bodies are
    skipped on both sides — they carry no signal for triage.
    """
    last_agent_idx: int | None = None
    for idx in range(len(comments) - 1, -1, -1):
        if is_bot_comment(comments[idx], bot_user_ids):
            last_agent_idx = idx
            break
    if last_agent_idx is None:
        return None, []
    agent_body = (comments[last_agent_idx].body or '').strip()
    if not agent_body:
        return None, []
    replies = [
        (c.body or '').strip()
        for c in comments[last_agent_idx + 1 :]
        if not is_bot_comment(c, bot_user_ids) and (c.body or '').strip()
    ]
    return agent_body, replies


_HUMAN_GATED_STATES: tuple[str, ...] = ('Awaiting approval', 'Triage', 'Backlog')


def _list_human_gated_unassigned(
    linear: LinearClient, team_id: str, project_id: str | None = None
) -> list[Issue]:
    """Issues sitting on a human input gate, regardless of label.

    Three columns are interesting:

    - `Awaiting approval`: branch by label.
    - `Triage` (no parent): only when gated by `awaiting-human-reply`;
      ungated Triage is naturally re-pickable by Architect.
    - `Backlog`: only when gated by `awaiting-human-reply`; ungated
      Backlog is naturally re-pickable by Coder.

    Filtering for `assignee=null` happens in Python — Linear's
    `assignee: {null: true}` filter has been observed to silently drop
    valid candidates, and we want a stable inspect count for logging.
    """
    document = (
        'query HumanGated($filter: IssueFilter!) { '
        'issues(filter: $filter, first: 100) { nodes {' + ISSUE_FRAGMENT + '} } }'
    )
    filt: dict[str, Any] = {
        'team': {'id': {'eq': team_id}},
        'state': {'name': {'in': list(_HUMAN_GATED_STATES)}},
    }
    if project_id is not None:
        filt['project'] = {'id': {'eq': project_id}}
    data = linear.query(document, {'filter': filt})
    nodes = data['issues']['nodes']
    return [issue_from_node(node) for node in nodes if node.get('assignee') is None]


def _states_for_team(linear: LinearClient, team_id: str) -> dict[str, str]:
    document = """
        query States($id: String!) {
          team(id: $id) {
            states { nodes { id name } }
          }
        }
    """
    data = linear.query(document, {'id': team_id})
    nodes = data['team']['states']['nodes']
    return {n['name']: n['id'] for n in nodes}


def _try_redispatch(
    *,
    linear: LinearClient,
    issue: Issue,
    states_by_name: dict[str, str],
) -> RedispatchResult | None:
    label_lookup = _team_labels_for_issue(linear, issue)
    target_state, trigger_label = _resolve_target(issue)
    if target_state is None or trigger_label is None:
        log.info(
            'redispatch: %s in %s has no recognised gating label (%s) — skipping',
            issue.identifier,
            issue.state_name,
            ','.join(issue.label_names) or '<none>',
        )
        return None

    new_label_ids = _strip_pickup_blockers(issue.label_ids, label_lookup)
    new_label_ids = _strip_label(new_label_ids, label_lookup, trigger_label)

    if trigger_label == _DECOMPOSITION_LABEL:
        try:
            children = linear.list_children(issue.id)
        except Exception as exc:
            log.warning(
                'redispatch: list_children for %s failed: %s', issue.identifier, exc
            )
            children = []
        for child in children:
            try:
                linear.archive_issue(child.id)
            except Exception as exc:
                log.warning('redispatch: archive %s failed: %s', child.identifier, exc)

    if target_state == _STAY:
        update = IssueUpdate(label_ids=new_label_ids, unset_assignee=True)
        reported_to = issue.state_name
    else:
        state_id = states_by_name.get(target_state)
        if state_id is None:
            log.warning(
                'redispatch: target state %r not found for team — skipping %s',
                target_state,
                issue.identifier,
            )
            return None
        update = IssueUpdate(
            state_id=state_id,
            label_ids=new_label_ids,
            unset_assignee=True,
        )
        reported_to = target_state

    try:
        linear.update_issue(issue.id, update)
    except Exception as exc:
        log.warning('redispatch: update for %s failed: %s', issue.identifier, exc)
        return None
    log.info(
        'redispatch: %s %s -> %s on %s',
        issue.identifier,
        issue.state_name,
        reported_to,
        trigger_label,
    )
    return RedispatchResult(
        issue_identifier=issue.identifier,
        from_state=issue.state_name,
        to_state=reported_to,
        trigger_label=trigger_label,
    )


def _resolve_target(issue: Issue) -> tuple[str | None, str | None]:
    """Map an issue's state + labels to a target column and gating label.

    `Triage`/`Backlog` only re-dispatch when the `awaiting-human-reply`
    label is set — ungated Triage/Backlog issues are already re-pickable
    by workers, so touching them here would be churn. For Triage we also
    require `parent_id is None`: child issues are gated by parent
    approval, not by user comments on themselves.

    `Awaiting approval` branches by label. Order matters:
    `kind:decomposition` is checked first because an architect parent
    that also accumulated `stuck` (from a downstream child) should still
    re-architect, not be sent to Backlog.
    """
    label_names = issue.label_names
    if issue.state_name == 'Awaiting approval':
        if _DECOMPOSITION_LABEL in label_names:
            return 'Triage', _DECOMPOSITION_LABEL
        if _FINAL_PR_LABEL in label_names:
            return 'Review', _FINAL_PR_LABEL
        if _STUCK_LABEL in label_names:
            return 'Backlog', _STUCK_LABEL
        return None, None
    if _AWAIT_HUMAN_LABEL in label_names:
        if issue.state_name == 'Triage' and issue.parent_id is not None:
            return None, None
        if issue.state_name in ('Triage', 'Backlog'):
            return _STAY, _AWAIT_HUMAN_LABEL
    return None, None


def _strip_pickup_blockers(
    current_ids: tuple[str, ...],
    label_lookup: dict[str, str],
) -> tuple[str, ...]:
    blocker_ids = {
        lid for name, lid in label_lookup.items() if name in _PICKUP_BLOCKERS
    }
    return tuple(lid for lid in current_ids if lid not in blocker_ids)


def _strip_label(
    current_ids: tuple[str, ...],
    label_lookup: dict[str, str],
    label_name: str,
) -> tuple[str, ...]:
    target = label_lookup.get(label_name)
    if target is None:
        return current_ids
    return tuple(lid for lid in current_ids if lid != target)


def _team_labels_for_issue(linear: LinearClient, issue: Issue) -> dict[str, str]:
    document = """
        query LabelsForIssue($id: String!) {
          issue(id: $id) {
            team { labels(first: 250) { nodes { id name } } }
          }
        }
    """
    data = linear.query(document, {'id': issue.identifier})
    nodes = data['issue']['team']['labels']['nodes']
    return {n['name']: n['id'] for n in nodes}


def _latest_id(comments: list[Comment]) -> str:
    """`list_comments` returns oldest-first per the Linear default order."""
    return comments[-1].id


def _load_seen(path: Path) -> dict[str, str]:
    """Read the per-issue last-seen-comment map, tolerating absence/corruption."""
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning('redispatch state read failed at %s: %s', path, exc)
        return {}
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning('redispatch state at %s is corrupt: %s', path, exc)
        return {}
    if not isinstance(parsed, dict):
        return {}
    data = cast(dict[str, Any], parsed)
    if data.get('version') != LAST_COMMENT_STATE_VERSION:
        return {}
    seen_raw = data.get('seen', {})
    if not isinstance(seen_raw, dict):
        return {}
    seen_typed = cast(dict[str, Any], seen_raw)
    return {str(k): str(v) for k, v in seen_typed.items()}


def _save_seen(path: Path, seen: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'version': LAST_COMMENT_STATE_VERSION, 'seen': seen}
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload), encoding='utf-8')
    tmp.replace(path)
