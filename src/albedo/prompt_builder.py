"""Prompt assembly: common prefix + role-specific block.

Templates live in `prompts/`. Each role has a Jinja2 template that consumes
the same variable set as the common prefix; the rendered output is
concatenated and fed verbatim to `claude -p`.

`StrictUndefined` is intentional: a missing variable is a programmer error,
not a soft warning. Loud failure is the right default here.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
)
from jinja2 import (
    TemplateError as JinjaTemplateError,
)
from jinja2 import (
    TemplateNotFound as JinjaTemplateNotFound,
)

from albedo import paths

COMMON_PREFIX_TEMPLATE = 'common-prefix.md.tmpl'


def default_prompts_dir() -> Path:
    return paths.prompts_dir()


class PromptBuildError(RuntimeError):
    """Raised when prompt rendering fails (missing template/variable)."""


@dataclass(frozen=True, slots=True)
class PromptContext:
    """Variables shared between common prefix and role-specific block."""

    agent_id: str
    issue_id: str
    title: str
    description: str
    acceptance_criteria: tuple[str, ...]
    column: str
    role: str
    worktree_path: str
    branch: str
    base_branch: str
    repo_name: str
    allowed_tools: str
    target_column_on_success: str
    target_column_on_blocker: str
    role_timeout_minutes: int
    parent_id: str | None
    attempts: int
    pr_url: str | None = None
    user_comments_block: str = ''
    attachments_block: str = ''
    reviewer_feedback_block: str = ''
    reviewer_findings_block: str = ''


class PromptBuilder:
    """Render orchestrator prompts from `prompts/` Jinja templates.

    `prompts_dir` accepts either a single directory (back-compat) or an
    ordered iterable of directories. With multiple dirs, Jinja's
    `FileSystemLoader` picks the first match, so callers should pass
    project-local first and global fallback second.
    """

    def __init__(
        self,
        prompts_dir: Path | str | Iterable[Path | str] | None = None,
    ) -> None:
        self._prompts_dirs = _normalize_prompts_dirs(prompts_dir)
        self._env = Environment(
            loader=FileSystemLoader([str(p) for p in self._prompts_dirs]),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            autoescape=False,
        )

    def build(self, role_template: str, context: PromptContext) -> str:
        variables = _context_to_variables(context)
        prefix = self._render(COMMON_PREFIX_TEMPLATE, variables)
        body = self._render(role_template, variables)
        return f'{prefix}\n{body}'

    def _render(self, template_name: str, variables: dict[str, object]) -> str:
        try:
            template = self._env.get_template(template_name)
        except JinjaTemplateNotFound as exc:
            searched = ', '.join(str(p) for p in self._prompts_dirs)
            raise PromptBuildError(
                f'Prompt template {template_name!r} not found in {searched}'
            ) from exc
        try:
            return template.render(**variables)
        except JinjaTemplateError as exc:
            raise PromptBuildError(
                f'Failed to render prompt template {template_name!r}: {exc}'
            ) from exc


def _normalize_prompts_dirs(
    prompts_dir: Path | str | Iterable[Path | str] | None,
) -> tuple[Path, ...]:
    if prompts_dir is None:
        return (default_prompts_dir(),)
    if isinstance(prompts_dir, (str, Path)):
        return (Path(prompts_dir),)
    return tuple(Path(p) for p in prompts_dir)


def _context_to_variables(context: PromptContext) -> dict[str, object]:
    return {
        'agent_id': context.agent_id,
        'issue_id': context.issue_id,
        'title': context.title,
        'description': context.description,
        'description_indented': _indent(context.description),
        'ac_bullets': _ac_bullets(context.acceptance_criteria),
        'column': context.column,
        'role': context.role,
        'worktree_path': context.worktree_path,
        'branch': context.branch,
        'base_branch': context.base_branch,
        'repo_name': context.repo_name,
        'allowed_tools': context.allowed_tools,
        'target_column_on_success': context.target_column_on_success,
        'target_column_on_blocker': context.target_column_on_blocker,
        'role_timeout_minutes': context.role_timeout_minutes,
        'parent_id': context.parent_id,
        'attempts': context.attempts,
        'pr_url': context.pr_url or '',
        'user_comments_block': context.user_comments_block,
        'attachments_block': context.attachments_block,
        'reviewer_feedback_block': context.reviewer_feedback_block,
        'reviewer_findings_block': context.reviewer_findings_block,
    }


def _indent(text: str, *, prefix: str = '    ') -> str:
    return textwrap.indent(text.rstrip('\n'), prefix=prefix) if text else f'{prefix}—'


def _ac_bullets(items: Iterable[str]) -> str:
    rendered = [f'    - {item.strip()}' for item in items if item.strip()]
    return '\n'.join(rendered) if rendered else '    - (no AC declared)'
