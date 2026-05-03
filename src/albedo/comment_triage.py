"""LLM-based gate that decides whether a user reply warrants redispatch.

When an agent parks an issue (BLOCKED question, awaiting approval, ready
for merge), `comment_redispatch` otherwise re-fires on every new human
comment. Some replies — "let me think", "ok", emoji reactions — contain
nothing to act on, and re-firing on them produces visible churn: state
flips, the worker re-spawns, the agent posts another clarifying message.

This module asks Claude itself whether the reply substantively engages
with the agent's last message. The check uses `claude -p` with no tools,
no MCP, and `--max-turns 1`, so it's cheap relative to a full worker run.

Failure mode is fail-open: any parse/runtime/timeout error returns
`is_substantive=True` so a triage bug never silently swallows real input.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from albedo.claude_runner import (
    DEFAULT_CLI,
    ClaudeRunError,
    ClaudeRunResult,
    spawn_claude,
)

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 90

SpawnFn = Callable[..., ClaudeRunResult]

_VERDICT_RE = re.compile(r'VERDICT\s*:\s*(YES|NO)\b', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class TriageVerdict:
    is_substantive: bool
    reason: str


def triage_user_reply(
    *,
    agent_message: str,
    user_replies: Sequence[str],
    cwd: Path,
    cli: str = DEFAULT_CLI,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    spawn: SpawnFn = spawn_claude,
    model: str | None = None,
) -> TriageVerdict:
    """Decide whether `user_replies` substantively engage with `agent_message`.

    Calls `claude -p` with no tools and a strict YES/NO prompt. On any
    error (timeout, parse failure, missing claude CLI), returns
    `is_substantive=True` — letting redispatch fire is a safer default
    than dropping a real user answer because of an infrastructure issue.
    """
    if not user_replies:
        return TriageVerdict(is_substantive=False, reason='no user replies')

    prompt = _build_prompt(agent_message, user_replies)
    try:
        result = spawn(
            prompt=prompt,
            cwd=cwd,
            allowed_tools=[],
            mcp_config_path=None,
            max_turns=1,
            timeout_seconds=timeout_seconds,
            cli=cli,
            model=model,
        )
    except ClaudeRunError as exc:
        log.warning('triage: claude run failed (%s) — fail-open YES', exc)
        return TriageVerdict(is_substantive=True, reason=f'triage error: {exc}')
    except Exception as exc:
        log.warning('triage: unexpected error (%s) — fail-open YES', exc)
        return TriageVerdict(is_substantive=True, reason=f'triage error: {exc}')

    if result.timed_out:
        log.warning('triage: timed out after %ds — fail-open YES', timeout_seconds)
        return TriageVerdict(is_substantive=True, reason='triage timed out')
    if result.is_error:
        log.warning(
            'triage: claude reported error (exit=%d) — fail-open YES',
            result.exit_code,
        )
        return TriageVerdict(is_substantive=True, reason='triage claude error')

    return _parse_verdict(result.result_text)


def _parse_verdict(text: str) -> TriageVerdict:
    """Find the first `VERDICT: YES|NO -- reason` line in the model's output."""
    for line in text.splitlines():
        match = _VERDICT_RE.search(line)
        if match is None:
            continue
        verdict = match.group(1).upper() == 'YES'
        reason = line[match.end() :].lstrip(' -:').strip() or '<no reason given>'
        return TriageVerdict(is_substantive=verdict, reason=reason)
    log.warning(
        'triage: could not parse VERDICT from model output (%r) — fail-open YES',
        text[:200],
    )
    return TriageVerdict(is_substantive=True, reason='unparseable verdict')


def _build_prompt(agent_message: str, user_replies: Sequence[str]) -> str:
    """Compose the triage prompt. Kept as a module-level function for testability."""
    rendered_replies = '\n\n---\n\n'.join(reply.strip() for reply in user_replies)
    return _PROMPT_TEMPLATE.format(
        agent_message=agent_message.strip(),
        user_replies=rendered_replies,
    )


_PROMPT_TEMPLATE = """\
You are a triage gate for an autonomous AI orchestrator. An AI agent \
posted a message on a Linear issue (asking for input, announcing a PR \
ready for merge, or surfacing a blocker), and a human replied. You must \
decide whether to re-route the issue back to the AI worker pool now, or \
leave it parked until the human says something more substantive.

SUBSTANTIVE — re-route now. The human:
- answered the agent's question (even partially or with a counter-question \
  that needs the agent to act)
- gave a decision, approval, rejection, or new direction
- provided new constraints, requirements, or information the agent needs
- pushed the work forward in any concrete way

NOT SUBSTANTIVE — leave parked. The human:
- only acknowledged ("ok", "thanks", "I see", "noted")
- deferred ("let me think", "later", "tbd", "tomorrow", "надо подумать")
- reacted without content (emoji-only, "...", "hmm")
- gave generic encouragement that doesn't approve or decide
- asked an unrelated meta-question that doesn't engage with the agent

Be strict. False NO is cheap — we re-evaluate on the next human comment. \
False YES is expensive — it triggers state changes, a worker spawn, and \
often another round of agent questions to the human.

Reply with EXACTLY one line, no preamble, no markdown:
VERDICT: YES|NO -- <one short reason>

==== AGENT MESSAGE ====
{agent_message}

==== HUMAN REPLY (most recent last) ====
{user_replies}
"""
