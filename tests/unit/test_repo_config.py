import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from albedo.config import GithubConfig
from albedo.repo_config import (
    RepoManifest,
    RepoManifestLinear,
    detect_github_from_git,
)


def test_repo_manifest_rejects_placeholder_name() -> None:
    with pytest.raises(ValidationError, match='REPLACE_ME'):
        RepoManifest(name='REPLACE_ME', linear=RepoManifestLinear(project='X'))


def test_repo_manifest_rejects_placeholder_linear_project() -> None:
    with pytest.raises(ValidationError, match='REPLACE_ME'):
        RepoManifest(name='ok', linear=RepoManifestLinear(project='REPLACE_ME'))


def test_github_config_rejects_placeholder_owner_repo() -> None:
    with pytest.raises(ValidationError, match='REPLACE_ME'):
        GithubConfig(owner='REPLACE_ME', repo='ok')
    with pytest.raises(ValidationError, match='REPLACE_ME'):
        GithubConfig(owner='ok', repo='REPLACE_ME')


def _init_git_repo_with_remote(path: Path, remote: str) -> None:
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    subprocess.run(
        ['git', '-C', str(path), 'remote', 'add', 'origin', remote],
        check=True,
    )


def test_detect_github_from_git_https(tmp_path: Path) -> None:
    _init_git_repo_with_remote(tmp_path, 'https://github.com/octocat/hello.git')
    assert detect_github_from_git(tmp_path) == ('octocat', 'hello')


def test_detect_github_from_git_ssh(tmp_path: Path) -> None:
    _init_git_repo_with_remote(tmp_path, 'git@github.com:octocat/spoon-knife.git')
    assert detect_github_from_git(tmp_path) == ('octocat', 'spoon-knife')


def test_detect_github_from_git_https_no_dot_git(tmp_path: Path) -> None:
    _init_git_repo_with_remote(tmp_path, 'https://github.com/octocat/hello')
    assert detect_github_from_git(tmp_path) == ('octocat', 'hello')


def test_detect_github_from_git_returns_none_for_non_github(tmp_path: Path) -> None:
    _init_git_repo_with_remote(tmp_path, 'https://gitlab.com/foo/bar.git')
    assert detect_github_from_git(tmp_path) is None


def test_detect_github_from_git_returns_none_when_not_a_repo(tmp_path: Path) -> None:
    assert detect_github_from_git(tmp_path) is None


def test_detect_github_from_git_returns_none_when_git_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError('git')

    monkeypatch.setattr(subprocess, 'run', boom)
    assert detect_github_from_git(tmp_path) is None
