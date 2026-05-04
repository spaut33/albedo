"""Tests for worktree CRUD against a real local git repo.

We initialize a tiny throwaway repo per test (with a single commit on `main`),
since git worktree behavior is hard to mock convincingly. These tests run in
milliseconds — they don't touch the network.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from albedo.worktree import (
    WorktreeError,
    branch_for_issue,
    ensure_worktree,
    list_worktrees,
    remove_worktree,
    worktree_path,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if not shutil.which('git'):
        pytest.skip('git is not available')
    upstream = tmp_path / 'upstream.git'
    workdir = tmp_path / 'repo'

    subprocess.run(['git', 'init', '--bare', str(upstream)], check=True)
    subprocess.run(
        ['git', 'clone', str(upstream), str(workdir)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ['git', '-C', str(workdir), 'config', 'user.email', 'test@example.com'],
        check=True,
    )
    subprocess.run(
        ['git', '-C', str(workdir), 'config', 'user.name', 'Test'],
        check=True,
    )
    (workdir / 'README.md').write_text('hello\n', encoding='utf-8')
    subprocess.run(['git', '-C', str(workdir), 'add', '.'], check=True)
    subprocess.run(
        ['git', '-C', str(workdir), 'commit', '-m', 'init'],
        check=True,
        capture_output=True,
    )
    subprocess.run(['git', '-C', str(workdir), 'branch', '-M', 'main'], check=True)
    subprocess.run(
        ['git', '-C', str(workdir), 'push', '-u', 'origin', 'main'],
        check=True,
        capture_output=True,
    )
    return workdir


def test_branch_for_issue_lowercases() -> None:
    assert branch_for_issue('AI-5') == 'task/ai-5'


def test_worktree_path_assembles() -> None:
    p = worktree_path(Path('/tmp/wt'), 'sample', 'AI-5')
    assert p == Path('/tmp/wt/sample-ai-5')


def test_ensure_worktree_creates_branch_and_dir(repo: Path, tmp_path: Path) -> None:
    wt_root = tmp_path / 'worktrees'
    info = ensure_worktree(repo, wt_root, 'sample', 'AI-5', 'main')
    assert info.path.exists()
    assert info.branch == 'task/ai-5'
    assert (info.path / 'README.md').exists()
    head = subprocess.run(
        ['git', '-C', str(info.path), 'rev-parse', '--abbrev-ref', 'HEAD'],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == 'task/ai-5'


def test_ensure_worktree_is_idempotent_when_branch_matches(
    repo: Path, tmp_path: Path
) -> None:
    wt_root = tmp_path / 'worktrees'
    first = ensure_worktree(repo, wt_root, 'sample', 'AI-5', 'main')
    second = ensure_worktree(repo, wt_root, 'sample', 'AI-5', 'main', fetch=False)
    assert first.path == second.path


def test_ensure_worktree_rejects_path_with_wrong_branch(
    repo: Path, tmp_path: Path
) -> None:
    wt_root = tmp_path / 'worktrees'
    info = ensure_worktree(repo, wt_root, 'sample', 'AI-5', 'main')
    subprocess.run(
        ['git', '-C', str(info.path), 'checkout', '-b', 'other'],
        check=True,
        capture_output=True,
    )
    with pytest.raises(WorktreeError, match='expected'):
        ensure_worktree(repo, wt_root, 'sample', 'AI-5', 'main', fetch=False)


def test_ensure_worktree_raises_on_invalid_base_branch(
    repo: Path, tmp_path: Path
) -> None:
    wt_root = tmp_path / 'worktrees'
    with pytest.raises(WorktreeError):
        ensure_worktree(repo, wt_root, 'sample', 'AI-7', 'no-such-branch', fetch=False)


def _local_git_config(worktree: Path, key: str) -> str | None:
    result = subprocess.run(
        ['git', '-C', str(worktree), 'config', '--local', '--get', key],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def test_ensure_worktree_stamps_bot_identity(repo: Path, tmp_path: Path) -> None:
    wt_root = tmp_path / 'worktrees'
    info = ensure_worktree(
        repo,
        wt_root,
        'sample',
        'AI-5',
        'main',
        bot_identity=('Albedo Bot', 'bot@example.com'),
    )
    assert _local_git_config(info.path, 'user.name') == 'Albedo Bot'
    assert _local_git_config(info.path, 'user.email') == 'bot@example.com'


def test_ensure_worktree_re_stamps_on_reuse(repo: Path, tmp_path: Path) -> None:
    wt_root = tmp_path / 'worktrees'
    first = ensure_worktree(
        repo,
        wt_root,
        'sample',
        'AI-5',
        'main',
        bot_identity=('Old Bot', 'old@example.com'),
    )
    second = ensure_worktree(
        repo,
        wt_root,
        'sample',
        'AI-5',
        'main',
        fetch=False,
        bot_identity=('New Bot', 'new@example.com'),
    )
    assert first.path == second.path
    assert _local_git_config(second.path, 'user.name') == 'New Bot'
    assert _local_git_config(second.path, 'user.email') == 'new@example.com'


def test_ensure_worktree_inherits_existing_config_when_no_identity(
    repo: Path, tmp_path: Path
) -> None:
    wt_root = tmp_path / 'worktrees'
    info = ensure_worktree(repo, wt_root, 'sample', 'AI-5', 'main')
    # No bot_identity passed → we must not have overwritten the parent
    # repo's user.email; the worktree continues to inherit it.
    inherited = subprocess.run(
        ['git', '-C', str(info.path), 'config', '--get', 'user.email'],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert inherited == 'test@example.com'


def test_remove_worktree_deletes_path(repo: Path, tmp_path: Path) -> None:
    wt_root = tmp_path / 'worktrees'
    info = ensure_worktree(repo, wt_root, 'sample', 'AI-5', 'main')
    remove_worktree(repo, info.path)
    assert not info.path.exists()


def test_remove_worktree_force_removes_dirty_state(repo: Path, tmp_path: Path) -> None:
    wt_root = tmp_path / 'worktrees'
    info = ensure_worktree(repo, wt_root, 'sample', 'AI-5', 'main')
    (info.path / 'dirty.txt').write_text('uncommitted\n', encoding='utf-8')
    remove_worktree(repo, info.path, force=True)
    assert not info.path.exists()


def test_remove_worktree_is_noop_when_missing(repo: Path, tmp_path: Path) -> None:
    remove_worktree(repo, tmp_path / 'nope')


def test_list_worktrees_returns_only_task_worktrees(repo: Path, tmp_path: Path) -> None:
    wt_root = tmp_path / 'worktrees'
    ensure_worktree(repo, wt_root, 'sample', 'AI-5', 'main')
    ensure_worktree(repo, wt_root, 'sample', 'AI-6', 'main', fetch=False)
    listed = list_worktrees(repo)
    branches = [w.branch for w in listed]
    assert 'task/ai-5' in branches
    assert 'task/ai-6' in branches
    assert all(w.branch.startswith('task/') for w in listed)


def test_run_git_timeout_raises(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from albedo import worktree as wt_mod

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd='git', timeout=1)

    monkeypatch.setattr(wt_mod.subprocess, 'run', fake_run)
    with pytest.raises(WorktreeError, match='timed out'):
        ensure_worktree(repo, Path('/tmp'), 'sample', 'AI-9', 'main', fetch=False)
