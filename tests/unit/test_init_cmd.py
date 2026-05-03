import subprocess
from pathlib import Path

import pytest

from albedo import paths
from albedo.init_cmd import (
    init_main,
    init_repo_main,
    seed_global_home,
    seed_repo_manifest,
)


def test_seed_global_home_creates_all_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('ALBEDO_HOME', str(tmp_path))
    home = seed_global_home()
    assert home == tmp_path
    assert (tmp_path / 'config.yaml').is_file()
    assert (tmp_path / '.env').is_file()
    assert (tmp_path / 'mcp-servers.json').is_file()
    assert (tmp_path / 'prompts' / 'architect.md').is_file()
    assert (tmp_path / 'prompts' / 'common-prefix.md.tmpl').is_file()


def test_seed_global_home_is_idempotent_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('ALBEDO_HOME', str(tmp_path))
    seed_global_home()
    (tmp_path / 'config.yaml').write_text('# edited\n', encoding='utf-8')
    seed_global_home(force=False)
    assert (tmp_path / 'config.yaml').read_text() == '# edited\n'


def test_seed_global_home_force_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('ALBEDO_HOME', str(tmp_path))
    seed_global_home()
    (tmp_path / 'config.yaml').write_text('# edited\n', encoding='utf-8')
    seed_global_home(force=True)
    assert 'REPLACE_ME' in (tmp_path / 'config.yaml').read_text()


def test_seed_repo_manifest_uses_placeholder_when_no_git(tmp_path: Path) -> None:
    target = seed_repo_manifest(tmp_path)
    body = target.read_text(encoding='utf-8')
    assert 'name: REPLACE_ME' in body
    assert 'owner: REPLACE_ME' in body
    assert 'repo: REPLACE_ME' in body


def test_seed_repo_manifest_autodetects_github(tmp_path: Path) -> None:
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(
        [
            'git',
            '-C',
            str(tmp_path),
            'remote',
            'add',
            'origin',
            'https://github.com/octocat/hello.git',
        ],
        check=True,
    )
    target = seed_repo_manifest(tmp_path)
    body = target.read_text(encoding='utf-8')
    assert 'owner: octocat' in body
    assert 'repo: hello' in body
    # name and linear.project are still placeholders for the operator.
    assert 'name: REPLACE_ME' in body


def test_init_main_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('ALBEDO_HOME', str(tmp_path))
    assert init_main([]) == 0
    assert paths.global_config_path().is_file()


def test_init_repo_main_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert init_repo_main([]) == 0
    assert (tmp_path / '.albedo.yaml').is_file()
