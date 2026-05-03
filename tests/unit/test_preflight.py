"""Tests for the `preflight` subcommand and its individual checks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr

from albedo import preflight as pf
from albedo.config import LINEAR_API_KEY_ENV
from albedo.github_client import GithubClient, GithubError
from albedo.linear_client import LinearClient, LinearError, Viewer


def _completed(
    returncode: int = 0, stdout: str = '', stderr: str = ''
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_check_python_version_passes_on_current_interpreter() -> None:
    result = pf.check_python_version()
    assert result.ok
    assert f'{sys.version_info[0]}.{sys.version_info[1]}' in result.message


def test_check_python_version_fails_below_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pf, 'MIN_PYTHON', (99, 0))
    result = pf.check_python_version()
    assert not result.ok
    assert '99.0' in result.message
    assert result.hint


def test_check_git_version_passes() -> None:
    result = pf.check_git_version(
        run=lambda *a, **kw: _completed(stdout='git version 2.47.1')
    )
    assert result.ok
    assert 'git 2.47' in result.message


def test_check_git_version_fails_when_too_old() -> None:
    result = pf.check_git_version(
        run=lambda *a, **kw: _completed(stdout='git version 2.10.0')
    )
    assert not result.ok
    assert '2.10' in result.message


def test_check_git_version_fails_when_missing() -> None:
    def boom(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError('git')

    result = pf.check_git_version(run=boom)
    assert not result.ok
    assert 'git not found' in result.message


def test_check_git_version_fails_unparseable() -> None:
    result = pf.check_git_version(
        run=lambda *a, **kw: _completed(stdout='something weird')
    )
    assert not result.ok


def test_check_claude_binary_passes() -> None:
    result = pf.check_claude_binary(which=lambda _name: '/usr/bin/claude')
    assert result.ok
    assert '/usr/bin/claude' in result.message


def test_check_claude_binary_fails_when_missing() -> None:
    result = pf.check_claude_binary(which=lambda _name: None)
    assert not result.ok
    assert result.hint


def test_check_config_loads_passes(tmp_path: Path) -> None:
    cfg_path = tmp_path / 'config.yaml'
    cfg_path.write_text(
        'workers: 1\nlinear:\n  team: ORC\n',
        encoding='utf-8',
    )
    result = pf.check_config_loads(cfg_path)
    assert result.ok
    assert 'parsed' in result.message


def test_check_config_loads_fails_when_missing(tmp_path: Path) -> None:
    result = pf.check_config_loads(tmp_path / 'nope.yaml')
    assert not result.ok


def test_check_config_loads_fails_when_invalid(tmp_path: Path) -> None:
    bad = tmp_path / 'bad.yaml'
    bad.write_text('this: is: not yaml properly\n', encoding='utf-8')
    result = pf.check_config_loads(bad)
    assert not result.ok


def test_check_repo_and_worktree_passes(tmp_path: Path) -> None:
    repo_root = tmp_path / 'repo'
    repo_root.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo_root)], check=True)
    (repo_root / '.albedo.yaml').write_text(
        'name: sample\nlinear:\n  project: Sample\nrepo:\n  base_branch: main\n',
        encoding='utf-8',
    )
    cfg_path = tmp_path / 'config.yaml'
    cfg_path.write_text(
        f'workers: 1\nworktree_root: {tmp_path}/wt\nlinear:\n  team: ORC\n',
        encoding='utf-8',
    )
    cfg = pf.load_config(cfg_path)
    result = pf.check_repo_and_worktree(cfg, cwd=repo_root)
    assert result.ok, result.message + ' / ' + result.hint


def test_check_repo_and_worktree_reports_missing_repo(tmp_path: Path) -> None:
    # Manifest exists but its parent directory was removed before the check.
    repo_root = tmp_path / 'missing'
    repo_root.mkdir()
    (repo_root / '.albedo.yaml').write_text(
        'name: sample\nlinear:\n  project: Sample\nrepo:\n  base_branch: main\n',
        encoding='utf-8',
    )
    cfg_path = tmp_path / 'config.yaml'
    cfg_path.write_text(
        f'workers: 1\nworktree_root: {tmp_path}/wt\nlinear:\n  team: ORC\n',
        encoding='utf-8',
    )
    cfg = pf.load_config(cfg_path)

    # Simulate the repo dir disappearing after manifest discovery by
    # removing it before the check runs.
    import shutil

    cwd_for_walk = repo_root
    shutil.rmtree(repo_root)
    result = pf.check_repo_and_worktree(cfg, cwd=cwd_for_walk)
    assert not result.ok
    assert ('does not exist' in result.hint) or ('No .albedo.yaml found' in result.hint)


def test_check_repo_and_worktree_reports_missing_manifest(tmp_path: Path) -> None:
    cfg_path = tmp_path / 'config.yaml'
    cfg_path.write_text(
        f'workers: 1\nworktree_root: {tmp_path}/wt\nlinear:\n  team: ORC\n',
        encoding='utf-8',
    )
    cfg = pf.load_config(cfg_path)
    empty_dir = tmp_path / 'empty'
    empty_dir.mkdir()
    # Walk-up from empty_dir will reach tmp_path, /tmp, /, and find no manifest.
    # The loop also walks past tmp_path. To make sure we don't accidentally
    # pick up the project's own .albedo.yaml from a parent, set a very
    # constrained search start and confirm a no-manifest dir errors.
    result = pf.check_repo_and_worktree(cfg, cwd=empty_dir)
    assert not result.ok
    assert 'No .albedo.yaml found' in result.hint or 'does not exist' in result.hint


def test_check_mcp_config_present(tmp_path: Path) -> None:
    p = tmp_path / 'mcp.json'
    p.write_text('{}', encoding='utf-8')
    assert pf.check_mcp_config(p).ok


def test_check_mcp_config_missing(tmp_path: Path) -> None:
    result = pf.check_mcp_config(tmp_path / 'absent.json')
    assert not result.ok
    assert result.hint


class _FakeLinearClient:
    def __init__(
        self,
        *_a: object,
        viewer: Viewer | None = None,
        error: Exception | None = None,
        **_kw: object,
    ) -> None:
        self._viewer = viewer or Viewer(
            id='u1', name='Test User', email='t@example.com'
        )
        self._error = error

    def __enter__(self) -> _FakeLinearClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def viewer(self) -> Viewer:
        if self._error is not None:
            raise self._error
        return self._viewer


def _linear_factory(viewer_error: Exception | None = None) -> Any:
    def factory(*_a: object, **_kw: object) -> LinearClient:
        return cast(LinearClient, _FakeLinearClient(error=viewer_error))

    return factory


def test_check_linear_api_passes() -> None:
    result = pf.check_linear_api(
        env={LINEAR_API_KEY_ENV: 'lin_test'},
        client_factory=_linear_factory(),
    )
    assert result.ok
    assert 'Test User' in result.message


def test_check_linear_api_missing_key() -> None:
    result = pf.check_linear_api(env={})
    assert not result.ok
    assert LINEAR_API_KEY_ENV in result.hint


def test_check_linear_api_rejects_token() -> None:
    result = pf.check_linear_api(
        env={LINEAR_API_KEY_ENV: 'lin_test'},
        client_factory=_linear_factory(LinearError('401 unauthorized')),
    )
    assert not result.ok
    assert '401' in result.hint


class _FakeGithubClient:
    def __init__(
        self,
        *_a: object,
        login: str | None = 'octocat',
        error: Exception | None = None,
        **_kw: object,
    ) -> None:
        self._login = login
        self._error = error

    def __enter__(self) -> _FakeGithubClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get_authenticated_login(self) -> str:
        if self._error is not None:
            raise self._error
        assert self._login is not None
        return self._login


def _github_factory(login: str = 'octocat', error: Exception | None = None) -> Any:
    def factory(*_a: object, **_kw: object) -> GithubClient:
        return cast(GithubClient, _FakeGithubClient(login=login, error=error))

    return factory


def _stub_load_github_pat(value: SecretStr | None = None) -> Any:
    def loader(**_kw: object) -> SecretStr | None:
        return value

    return loader


def _stub_load_github_pat_raises(message: str) -> Any:
    def loader(**_kw: object) -> SecretStr | None:
        raise RuntimeError(message)

    return loader


def test_check_github_pat_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    pat = SecretStr('ghp_x')
    monkeypatch.setattr(pf, 'load_github_pat', _stub_load_github_pat(pat))
    result = pf.check_github_pat(client_factory=_github_factory())
    assert result.ok
    assert 'octocat' in result.message


def test_check_github_pat_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pf,
        'load_github_pat',
        _stub_load_github_pat_raises('GITHUB_PERSONAL_ACCESS_TOKEN is not set.'),
    )
    result = pf.check_github_pat()
    assert not result.ok
    assert 'GITHUB_PERSONAL_ACCESS_TOKEN' in result.message


def test_check_github_pat_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    pat = SecretStr('ghp_x')
    monkeypatch.setattr(pf, 'load_github_pat', _stub_load_github_pat(pat))
    result = pf.check_github_pat(
        client_factory=_github_factory(error=GithubError('Bad credentials'))
    )
    assert not result.ok
    assert 'Bad credentials' in result.hint


def _stub_build_checks(checks: list[pf.Check]) -> Any:
    def builder(**_kw: object) -> list[pf.Check]:
        return checks

    return builder


def test_run_preflight_returns_zero_on_all_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pf,
        'build_checks',
        _stub_build_checks([pf.Check('always-ok', lambda: pf.CheckResult(True, 'ok'))]),
    )
    assert pf.run_preflight() == 0


def test_run_preflight_returns_one_on_any_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pf,
        'build_checks',
        _stub_build_checks(
            [
                pf.Check('ok', lambda: pf.CheckResult(True, 'ok')),
                pf.Check(
                    'bad', lambda: pf.CheckResult(False, 'nope', hint='try again')
                ),
            ]
        ),
    )
    assert pf.run_preflight() == 1


def test_run_preflight_treats_exception_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raises() -> pf.CheckResult:
        raise RuntimeError('boom')

    monkeypatch.setattr(
        pf,
        'build_checks',
        _stub_build_checks([pf.Check('boom', raises)]),
    )
    assert pf.run_preflight() == 1
