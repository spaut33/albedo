"""Tests for the prompt assembly layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from albedo.prompt_builder import (
    COMMON_PREFIX_TEMPLATE,
    PromptBuilder,
    PromptBuildError,
    PromptContext,
)
from tests._prompts_dir import bundled_prompts_dir


def _ctx(**overrides: object) -> PromptContext:
    base: dict[str, object] = {
        'agent_id': '1',
        'issue_id': 'AI-5',
        'title': 'Add filter',
        'description': 'Filter admin users by role.',
        'acceptance_criteria': ('Adds /filter endpoint', 'Tests cover empty input'),
        'column': 'Backlog',
        'role': 'CODER',
        'worktree_path': '/tmp/wt/sample-ai-5',
        'branch': 'task/ai-5',
        'base_branch': 'main',
        'repo_name': 'sample',
        'allowed_tools': 'Edit,Bash,Read',
        'target_column_on_success': 'Review',
        'target_column_on_blocker': 'Backlog',
        'role_timeout_minutes': 30,
        'parent_id': None,
        'attempts': 0,
    }
    base.update(overrides)
    return PromptContext(**base)  # type: ignore[arg-type]


def test_build_with_real_templates() -> None:
    builder = PromptBuilder(prompts_dir=bundled_prompts_dir())
    rendered = builder.build('coder.md', _ctx())
    assert 'agent-1' in rendered
    assert 'AI-5' in rendered
    assert 'task/ai-5' in rendered
    assert 'CODER' in rendered
    assert 'main' in rendered
    assert 'Adds /filter endpoint' in rendered
    assert 'PR: https://github.com/' in rendered  # marker example from coder.md


def test_build_handles_empty_description_and_no_ac(tmp_path: Path) -> None:
    (tmp_path / 'common-prefix.md.tmpl').write_text(
        '{{ description_indented }}\n---\n{{ ac_bullets }}\n', encoding='utf-8'
    )
    (tmp_path / 'role.md').write_text('role: {{ issue_id }}\n', encoding='utf-8')
    builder = PromptBuilder(prompts_dir=tmp_path)
    rendered = builder.build(
        'role.md',
        _ctx(description='', acceptance_criteria=()),
    )
    assert '    —' in rendered
    assert '    - (no AC declared)' in rendered
    assert 'role: AI-5' in rendered


def test_build_renders_parent_id_when_present(tmp_path: Path) -> None:
    (tmp_path / 'common-prefix.md.tmpl').write_text(
        'parent={{ parent_id }}\n', encoding='utf-8'
    )
    (tmp_path / 'role.md').write_text('x', encoding='utf-8')
    builder = PromptBuilder(prompts_dir=tmp_path)
    rendered = builder.build('role.md', _ctx(parent_id='uuid-parent'))
    assert 'parent=uuid-parent' in rendered


def test_strict_undefined_raises_when_template_uses_unknown_var(
    tmp_path: Path,
) -> None:
    (tmp_path / 'common-prefix.md.tmpl').write_text('{{ ghost }}\n', encoding='utf-8')
    (tmp_path / 'role.md').write_text('x', encoding='utf-8')
    builder = PromptBuilder(prompts_dir=tmp_path)
    with pytest.raises(Exception) as exc:
        builder.build('role.md', _ctx())
    assert 'ghost' in str(exc.value)


def test_missing_template_raises_typed_error(tmp_path: Path) -> None:
    (tmp_path / 'common-prefix.md.tmpl').write_text('hi', encoding='utf-8')
    builder = PromptBuilder(prompts_dir=tmp_path)
    with pytest.raises(PromptBuildError, match='not found'):
        builder.build('does-not-exist.md', _ctx())


def test_search_path_project_local_overrides_global(tmp_path: Path) -> None:
    (tmp_path / 'coder.md').write_text('LOCAL CODER {{ issue_id }}', encoding='utf-8')
    builder = PromptBuilder(prompts_dir=[tmp_path, bundled_prompts_dir()])
    rendered = builder.build('coder.md', _ctx())
    assert 'LOCAL CODER AI-5' in rendered


def test_search_path_falls_back_to_global_when_local_missing_template(
    tmp_path: Path,
) -> None:
    (tmp_path / 'coder.md').write_text('LOCAL CODER {{ issue_id }}', encoding='utf-8')
    builder = PromptBuilder(prompts_dir=[tmp_path, bundled_prompts_dir()])
    rendered = builder.build('coder.md', _ctx())
    assert 'LOCAL CODER AI-5' in rendered
    assert 'agent-1' in rendered  # rendered from global common-prefix.md.tmpl


def test_search_path_raises_when_template_missing_in_all_dirs(
    tmp_path: Path,
) -> None:
    local = tmp_path / 'local'
    global_ = tmp_path / 'global'
    local.mkdir()
    global_.mkdir()
    (local / 'common-prefix.md.tmpl').write_text('hi', encoding='utf-8')
    (global_ / 'common-prefix.md.tmpl').write_text('hi', encoding='utf-8')
    builder = PromptBuilder(prompts_dir=[local, global_])
    with pytest.raises(PromptBuildError, match='not found'):
        builder.build('missing-role.md', _ctx())


def test_common_prefix_template_constant() -> None:
    assert COMMON_PREFIX_TEMPLATE == 'common-prefix.md.tmpl'


def test_attachments_block_omitted_when_empty() -> None:
    builder = PromptBuilder(prompts_dir=bundled_prompts_dir())
    rendered = builder.build('coder.md', _ctx(attachments_block=''))
    assert 'Attached files' not in rendered


def test_attachments_block_rendered_when_present() -> None:
    builder = PromptBuilder(prompts_dir=bundled_prompts_dir())
    block = '- .linear-attachments/AI-5/abc__mock.png (issue attachment)'
    rendered = builder.build('coder.md', _ctx(attachments_block=block))
    assert 'Attached files' in rendered
    assert block in rendered
    assert 'PDFs longer than 20 pages' in rendered
