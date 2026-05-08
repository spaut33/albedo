"""Tests for the bundled git credential helper.

Most of the helper is exercised directly as a Python function (`main()`) so we
can intercept stdin/stdout/stderr without spawning a subprocess. A separate
test in `test_worktree.py` covers the end-to-end protocol via real `git
credential fill`.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from albedo import git_credential

FIXTURE_PAT = 'ghp_fixturetokenAAAAAAAAAAAAAAAAAAAAAAAA'


@pytest.fixture(autouse=True)
def _isolated_albedo_home(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point `$ALBEDO_HOME` at a clean tmp dir for every test in this file."""
    home = tmp_path / 'albedo-home'
    monkeypatch.setenv('ALBEDO_HOME', str(home))
    # Drop any operator-supplied PAT so the helper has to consult `.env`.
    monkeypatch.delenv('GITHUB_PERSONAL_ACCESS_TOKEN', raising=False)
    return home


def _run_main(
    argv: list[str],
    *,
    stdin: str = '',
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, str, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    monkeypatch.setattr(sys, 'stdin', io.StringIO(stdin))
    monkeypatch.setattr(sys, 'stdout', out_buf)
    monkeypatch.setattr(sys, 'stderr', err_buf)
    code = git_credential.main(argv)
    return code, out_buf.getvalue(), err_buf.getvalue()


def test_get_returns_token_from_env_file(
    _isolated_albedo_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_albedo_home.mkdir(parents=True, exist_ok=True)
    (_isolated_albedo_home / '.env').write_text(
        f'GITHUB_PERSONAL_ACCESS_TOKEN={FIXTURE_PAT}\n', encoding='utf-8'
    )
    code, out, err = _run_main(
        ['albedo.git_credential', 'get'],
        stdin='protocol=https\nhost=github.com\n\n',
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert err == ''
    assert 'username=x-access-token\n' in out
    assert f'password={FIXTURE_PAT}\n' in out


def test_get_prefers_process_env_over_env_file(
    _isolated_albedo_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_albedo_home.mkdir(parents=True, exist_ok=True)
    (_isolated_albedo_home / '.env').write_text(
        'GITHUB_PERSONAL_ACCESS_TOKEN=ghp_from_file\n', encoding='utf-8'
    )
    monkeypatch.setenv('GITHUB_PERSONAL_ACCESS_TOKEN', 'ghp_from_env')
    _, out, _ = _run_main(['albedo.git_credential', 'get'], monkeypatch=monkeypatch)
    assert 'password=ghp_from_env' in out
    assert 'ghp_from_file' not in out


def test_get_fails_when_env_file_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, err = _run_main(
        ['albedo.git_credential', 'get'], monkeypatch=monkeypatch
    )
    assert code == 1
    assert out == ''
    assert 'not found' in err
    assert 'GITHUB_PERSONAL_ACCESS_TOKEN' in err


def test_get_fails_when_pat_key_missing(
    _isolated_albedo_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_albedo_home.mkdir(parents=True, exist_ok=True)
    (_isolated_albedo_home / '.env').write_text(
        'LINEAR_API_KEY=lin_test\n', encoding='utf-8'
    )
    code, out, err = _run_main(
        ['albedo.git_credential', 'get'], monkeypatch=monkeypatch
    )
    assert code == 1
    assert out == ''
    assert 'GITHUB_PERSONAL_ACCESS_TOKEN' in err
    assert 'not set' in err


def test_get_fails_when_pat_value_blank(
    _isolated_albedo_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_albedo_home.mkdir(parents=True, exist_ok=True)
    (_isolated_albedo_home / '.env').write_text(
        'GITHUB_PERSONAL_ACCESS_TOKEN=\n', encoding='utf-8'
    )
    code, _, err = _run_main(['albedo.git_credential', 'get'], monkeypatch=monkeypatch)
    assert code == 1
    assert 'GITHUB_PERSONAL_ACCESS_TOKEN' in err


def test_store_and_erase_are_noops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with no .env present, store/erase must not fail — git tolerates
    silent helpers, and we don't need to persist anything."""
    for action in ('store', 'erase'):
        code, out, err = _run_main(
            ['albedo.git_credential', action],
            stdin='username=x\npassword=y\n\n',
            monkeypatch=monkeypatch,
        )
        assert code == 0, f'{action} returned {code}: {err}'
        assert out == ''
        assert err == ''


def test_module_invocation_emits_credentials(
    _isolated_albedo_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke-test invoking via `python -m albedo.git_credential` (git's path)."""
    _isolated_albedo_home.mkdir(parents=True, exist_ok=True)
    (_isolated_albedo_home / '.env').write_text(
        f'GITHUB_PERSONAL_ACCESS_TOKEN={FIXTURE_PAT}\n', encoding='utf-8'
    )
    result = subprocess.run(
        [sys.executable, '-m', 'albedo.git_credential', 'get'],
        input='protocol=https\nhost=github.com\n\n',
        capture_output=True,
        text=True,
        timeout=10,
        env={
            **{
                k: v
                for k, v in os.environ.items()
                if k != 'GITHUB_PERSONAL_ACCESS_TOKEN'
            },
            'ALBEDO_HOME': str(_isolated_albedo_home),
        },
    )
    assert result.returncode == 0, result.stderr
    assert 'username=x-access-token' in result.stdout
    assert f'password={FIXTURE_PAT}' in result.stdout
