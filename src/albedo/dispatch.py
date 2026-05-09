"""Column → role dispatch table.

Each entry maps a Linear workflow state name to the role the orchestrator
should run, plus the parameters that role needs (allowed tools, target
columns, prompt template, time budget, --max-turns).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RoleSpec:
    role: str
    prompt_template: str
    allowed_tools: tuple[str, ...]
    target_state_on_success: str
    target_state_on_blocker: str
    timeout_minutes: int
    max_turns: int
    permission_mode: str | None = None


CODER = RoleSpec(
    role='CODER',
    prompt_template='coder.md',
    allowed_tools=(
        'Read',
        'Edit',
        'Write',
        'Glob',
        'Grep',
        'Bash',
        'Skill',
        'mcp__github__*',
    ),
    target_state_on_success='Review',
    target_state_on_blocker='Backlog',
    timeout_minutes=30,
    max_turns=100,
)

REVIEWER = RoleSpec(
    role='REVIEWER',
    prompt_template='reviewer.md',
    allowed_tools=(
        'Read',
        'Glob',
        'Grep',
        'Bash',
        'Task',
        'Skill',
        'mcp__github__*',
    ),
    target_state_on_success='Awaiting approval',
    target_state_on_blocker='Backlog',
    timeout_minutes=20,
    max_turns=50,
)

ARCHITECT = RoleSpec(
    role='ARCHITECT',
    prompt_template='architect.md',
    # Architect is read-only; it returns a structured proposal and the
    # orchestrator (Python) creates child issues, never claude.
    allowed_tools=(
        'Read',
        'Glob',
        'Grep',
        'Bash',
        'Task',
        'Skill',
    ),
    target_state_on_success='Awaiting approval',
    target_state_on_blocker='Triage',
    timeout_minutes=15,
    max_turns=40,
    permission_mode='plan',
)

DISPATCH: dict[str, RoleSpec] = {
    'Triage': ARCHITECT,
    'Backlog': CODER,
    'Review': REVIEWER,
}


class UnknownColumnError(RuntimeError):
    """Raised when a state name has no role mapping."""


def dispatch(state_name: str) -> RoleSpec:
    spec = DISPATCH.get(state_name)
    if spec is None:
        raise UnknownColumnError(
            f'No role mapped for Linear state {state_name!r}; '
            f'known states: {sorted(DISPATCH)}'
        )
    return spec
