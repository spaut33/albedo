"""Separate human-authored comments from albedo-posted ones.

Bot comments are filtered two ways:

1. By author identity: any `Comment.author_id` in the precomputed
   `bot_user_ids` set is dropped. The supervisor builds this set at startup
   by calling `viewer()` once per per-agent token plus the shared key.
2. By body prefix: comments posted via `worker._post_spawn_*` always start
   with `**agent-N**:`. The regex catches bot comments authored under a
   shared token whose viewer id wasn't aggregated, and is robust to future
   identity-discovery gaps.

A comment passing through the filter has neither signal of bot origin.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from albedo.linear_client import Comment

AGENT_PREFIX_RE = re.compile(r'^\s*\*\*(?:agent-\d+|housekeeping)\*\*\s*:')

REVIEWER_VERDICT_RE = re.compile(
    r'^\s*\*\*agent-\d+\*\*\s*:\s*(?:REVIEW\s+(?:APPROVE|REQUEST_CHANGES)|BLOCKED:)'
)


def is_bot_comment(comment: Comment, bot_user_ids: frozenset[str]) -> bool:
    """True if the comment was posted by an orchestrator-controlled token."""
    if comment.author_id and comment.author_id in bot_user_ids:
        return True
    return bool(AGENT_PREFIX_RE.match(comment.body or ''))


def filter_user_comments(
    comments: Iterable[Comment],
    bot_user_ids: frozenset[str],
) -> list[Comment]:
    """Return only human-authored comments, preserving input order."""
    return [c for c in comments if not is_bot_comment(c, bot_user_ids)]


def latest_reviewer_feedback(
    comments: Iterable[Comment],
    bot_user_ids: frozenset[str],
) -> Comment | None:
    """Return the most recent bot-authored reviewer-verdict comment, or None.

    `comments` is expected newest-last (the order returned by
    `LinearClient.list_comments`). Matches comments whose body opens with
    `**agent-N**:` followed by `REVIEW APPROVE`, `REVIEW REQUEST_CHANGES`,
    or `BLOCKED:` — the three shapes `_post_spawn_reviewer` writes.
    """
    for comment in reversed(list(comments)):
        if not is_bot_comment(comment, bot_user_ids):
            continue
        if REVIEWER_VERDICT_RE.match(comment.body or ''):
            return comment
    return None


def format_reviewer_feedback_block(comment: Comment | None) -> str:
    """Render a reviewer-feedback markdown block, or empty string if none.

    Quotes the comment body verbatim as a blockquote so the prompt template
    can drop it into a `## Reviewer feedback` section.
    """
    if comment is None:
        return ''
    body = (comment.body or '').strip()
    if not body:
        return ''
    quoted = '\n'.join(f'> {line}' if line else '>' for line in body.splitlines())
    return quoted


def format_user_comments_block(comments: Iterable[Comment]) -> str:
    """Render user comments as a markdown block, oldest first.

    Returns an empty string when there are no comments — callers can use
    this to suppress the surrounding section in the prompt template.
    """
    rendered: list[str] = []
    for comment in comments:
        author = comment.author_id or 'unknown'
        body = (comment.body or '').strip()
        if not body:
            continue
        indented = '\n'.join(f'      {line}' for line in body.splitlines())
        rendered.append(f'    - (author={author})\n{indented}')
    return '\n'.join(rendered)
