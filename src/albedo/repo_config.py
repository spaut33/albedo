"""Per-target-repo manifest discovered by walking up from CWD.

Each target repo carries a `.albedo.yaml` at its root with the fields
albedo needs to act on it: a stable `name` (used for state/worktree dir
slugs), the Linear project name to poll, and the GitHub coordinates for
PR ops. The repo path itself is implicit — it's the directory containing
the manifest.

`load_repo_manifest(start)` walks up from `start` until it finds the
file or hits the filesystem root, mirroring how `git`, `cargo`, and `uv`
locate their config. The error message points at `albedo init-repo`,
which writes a skeleton.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from albedo.config import GithubConfig, reject_placeholder

MANIFEST_FILENAME = '.albedo.yaml'

# git@github.com:owner/repo(.git)?  or  https://github.com/owner/repo(.git)?
_GITHUB_REMOTE_RE = re.compile(
    r'(?:git@github\.com:|https?://github\.com/)([^/]+)/([^/]+?)(?:\.git)?/?$'
)


class RepoManifestRepo(BaseModel):
    base_branch: str = Field(default='main')
    github: GithubConfig | None = Field(
        default=None,
        description='GitHub coordinates for the target repo. Required for E2E.',
    )


class RepoManifestLinear(BaseModel):
    project: str = Field(min_length=1, description='Exact Linear project name')

    @field_validator('project', mode='after')
    @classmethod
    def _no_placeholder(cls, value: str) -> str:
        return reject_placeholder(value, field='linear.project')


class RepoManifest(BaseModel):
    """Per-repo `.albedo.yaml` schema.

    `name` is the stable slug for `~/.local/state/albedo/<name>` and
    `~/.local/share/albedo/worktrees/<name>`. Renaming a repo's
    directory does not change it; only editing this field does.
    """

    name: str = Field(
        min_length=1,
        description='Stable project slug for state and worktree dirs.',
    )
    linear: RepoManifestLinear
    repo: RepoManifestRepo = Field(default_factory=RepoManifestRepo)

    @field_validator('name', mode='after')
    @classmethod
    def _no_placeholder(cls, value: str) -> str:
        return reject_placeholder(value, field='name')


class RepoManifestNotFoundError(RuntimeError):
    """Raised when walk-up from CWD never finds `.albedo.yaml`."""


def find_repo_manifest(start: Path) -> Path:
    """Walk up from `start` looking for `.albedo.yaml`.

    Returns the absolute path to the first match. Raises
    `RepoManifestNotFoundError` with an actionable hint if none is found
    before the filesystem root.
    """
    current = start.expanduser().absolute()
    visited: list[Path] = []
    while True:
        candidate = current / MANIFEST_FILENAME
        visited.append(current)
        if candidate.is_file():
            return candidate
        if current.parent == current:
            break
        current = current.parent
    raise RepoManifestNotFoundError(
        f'No {MANIFEST_FILENAME} found in {start} or any parent. '
        f"Run 'albedo init-repo' inside your target repo, or cd into one."
    )


def load_repo_manifest(start: Path) -> tuple[Path, RepoManifest]:
    """Discover and parse the manifest. Returns `(repo_root, manifest)`."""
    manifest_path = find_repo_manifest(start)
    raw: Any = yaml.safe_load(manifest_path.read_text(encoding='utf-8'))
    if not isinstance(raw, dict):
        raise ValueError(
            f'{manifest_path} root must be a mapping, got {type(raw).__name__}'
        )
    return manifest_path.parent, RepoManifest.model_validate(raw)


def detect_github_from_git(start: Path) -> tuple[str, str] | None:
    """Read `git -C <start> remote get-url origin` and parse owner/repo.

    Returns `(owner, repo)` or None if the command fails (no git, no
    remote, non-GitHub host, …). `albedo init-repo` falls back to
    placeholder values when this returns None.
    """
    try:
        result = subprocess.run(
            ['git', '-C', str(start), 'remote', 'get-url', 'origin'],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = _GITHUB_REMOTE_RE.search(result.stdout.strip())
    if match is None:
        return None
    return match.group(1), match.group(2)
