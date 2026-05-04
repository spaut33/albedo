"""Git worktree CRUD for the orchestrator.

One worktree per task: isolated working directory on branch `task/<issue-id>`,
forked from `origin/<base_branch>`. Created at pickup, removed after the task
reaches Done or Cancelled.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 60


class WorktreeError(RuntimeError):
    """Raised when a git worktree operation fails."""


@dataclass(frozen=True, slots=True)
class WorktreeInfo:
    path: Path
    branch: str
    base_branch: str


def branch_for_issue(issue_identifier: str) -> str:
    """Map a Linear identifier (e.g. 'AI-5') to a branch name."""
    return f'task/{issue_identifier.lower()}'


def worktree_path(worktree_root: Path, repo_name: str, issue_identifier: str) -> Path:
    return worktree_root / f'{repo_name}-{issue_identifier.lower()}'


def ensure_worktree(
    repo_path: Path,
    worktree_root: Path,
    repo_name: str,
    issue_identifier: str,
    base_branch: str,
    *,
    fetch: bool = True,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    bot_identity: tuple[str, str] | None = None,
) -> WorktreeInfo:
    """Create or reuse the worktree for a task.

    If the target path already exists and points to the expected branch, it's
    reused as-is. If the path is gone but the branch still exists (orphaned
    from a previous failed run), the branch is checked out into a fresh
    worktree without re-creating it. Otherwise the worktree is created with
    a brand-new branch off `origin/<base>`.

    When `bot_identity=(name, email)` is provided, the worktree's local
    `user.name`/`user.email` are stamped to those values so commits made
    inside it are attributed to the bot rather than the operator's global
    git config. Re-applied on every call so a config change takes effect on
    the next task without manual cleanup of existing worktrees.

    Raises `WorktreeError` if git refuses (path occupied by something else,
    fetch fails, etc.).
    """
    branch = branch_for_issue(issue_identifier)
    target = worktree_path(worktree_root, repo_name, issue_identifier)

    if target.exists():
        existing_branch = _current_branch(target, timeout_seconds=timeout_seconds)
        if existing_branch != branch:
            raise WorktreeError(
                f'Worktree at {target} is on branch {existing_branch!r}, '
                f'expected {branch!r}'
            )
        _apply_bot_identity(target, bot_identity, timeout_seconds=timeout_seconds)
        return WorktreeInfo(path=target, branch=branch, base_branch=base_branch)

    # Drop dangling worktree refs (e.g. dir was deleted manually) so the next
    # `worktree add` doesn't fail on stale metadata.
    _run_git(repo_path, ['worktree', 'prune'], timeout_seconds=timeout_seconds)

    target.parent.mkdir(parents=True, exist_ok=True)
    if fetch:
        _run_git(repo_path, ['fetch', 'origin'], timeout_seconds=timeout_seconds)

    if _branch_exists(repo_path, branch, timeout_seconds=timeout_seconds):
        # Reuse the existing branch — no `-b`.
        _run_git(
            repo_path,
            ['worktree', 'add', str(target), branch],
            timeout_seconds=timeout_seconds,
        )
    else:
        _run_git(
            repo_path,
            [
                'worktree',
                'add',
                '-b',
                branch,
                str(target),
                f'origin/{base_branch}',
            ],
            timeout_seconds=timeout_seconds,
        )
    _apply_bot_identity(target, bot_identity, timeout_seconds=timeout_seconds)
    return WorktreeInfo(path=target, branch=branch, base_branch=base_branch)


def _apply_bot_identity(
    target: Path,
    identity: tuple[str, str] | None,
    *,
    timeout_seconds: int,
) -> None:
    """Stamp `user.name`/`user.email` into the worktree's local git config.

    No-op when `identity is None` — the worktree falls through to the
    operator's global git config (the legacy behaviour).
    """
    if identity is None:
        return
    name, email = identity
    _run_git(target, ['config', 'user.name', name], timeout_seconds=timeout_seconds)
    _run_git(target, ['config', 'user.email', email], timeout_seconds=timeout_seconds)


def _branch_exists(repo_path: Path, branch: str, *, timeout_seconds: int) -> bool:
    try:
        _run_git(
            repo_path,
            ['show-ref', '--verify', '--quiet', f'refs/heads/{branch}'],
            timeout_seconds=timeout_seconds,
        )
    except WorktreeError:
        return False
    return True


def remove_worktree(
    repo_path: Path,
    worktree: Path,
    *,
    force: bool = False,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Remove a worktree. No-op if the path no longer exists."""
    if not worktree.exists():
        return
    args = ['worktree', 'remove', str(worktree)]
    if force:
        args.append('--force')
    _run_git(repo_path, args, timeout_seconds=timeout_seconds)


def has_unpushed_commits(
    worktree: Path,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """True if the worktree's HEAD is ahead of `@{upstream}` (or no upstream).

    Conservative: when the upstream cannot be resolved, return True so the
    caller leaves the worktree alone. We never want to delete code that
    might not be pushed yet.
    """
    try:
        upstream = _run_git(
            worktree,
            ['rev-parse', '--abbrev-ref', 'HEAD@{upstream}'],
            timeout_seconds=timeout_seconds,
        ).strip()
    except WorktreeError:
        return True
    if not upstream:
        return True
    try:
        ahead = _run_git(
            worktree,
            ['rev-list', '--count', f'{upstream}..HEAD'],
            timeout_seconds=timeout_seconds,
        ).strip()
    except WorktreeError:
        return True
    return ahead != '0'


def has_uncommitted_changes(
    worktree: Path,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """True if the worktree has any tracked-file changes or untracked files."""
    try:
        status = _run_git(
            worktree,
            ['status', '--porcelain=v1'],
            timeout_seconds=timeout_seconds,
        )
    except WorktreeError:
        return True
    return bool(status.strip())


def list_worktrees(
    repo_path: Path,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[WorktreeInfo]:
    """Parse `git worktree list --porcelain` into structured records.

    Empty entries (the porcelain format separates records with blank lines)
    and the main worktree are filtered out — only task worktrees are
    returned, sorted by path.
    """
    output = _run_git(
        repo_path, ['worktree', 'list', '--porcelain'], timeout_seconds=timeout_seconds
    )
    records: list[WorktreeInfo] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line:
            _maybe_record(records, current, repo_path)
            current = {}
            continue
        key, _, value = line.partition(' ')
        current[key] = value
    _maybe_record(records, current, repo_path)
    return sorted(records, key=lambda w: w.path)


def _maybe_record(
    records: list[WorktreeInfo], current: dict[str, str], repo_path: Path
) -> None:
    if 'worktree' not in current:
        return
    path = Path(current['worktree']).resolve()
    if path == repo_path.resolve():
        return  # main worktree
    branch_full = current.get('branch', '')
    branch = branch_full.removeprefix('refs/heads/') if branch_full else ''
    if not branch.startswith('task/'):
        return
    records.append(WorktreeInfo(path=path, branch=branch, base_branch=''))


def _current_branch(worktree: Path, *, timeout_seconds: int) -> str:
    return _run_git(
        worktree, ['rev-parse', '--abbrev-ref', 'HEAD'], timeout_seconds=timeout_seconds
    ).strip()


def _run_git(cwd: Path, args: list[str], *, timeout_seconds: int) -> str:
    try:
        result = subprocess.run(
            ['git', *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.CalledProcessError as exc:
        raise WorktreeError(
            f'git {" ".join(args)} failed (exit {exc.returncode}): {exc.stderr.strip()}'
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(
            f'git {" ".join(args)} timed out after {timeout_seconds}s'
        ) from exc
    return result.stdout
