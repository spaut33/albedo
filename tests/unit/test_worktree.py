"""Tests for worktree CRUD against a real local git repo.

We initialize a tiny throwaway repo per test (with a single commit on `main`),
since git worktree behavior is hard to mock convincingly. These tests run in
milliseconds — they don't touch the network.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import threading
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from albedo.worktree import (
    CREDENTIAL_URL_SCOPE,
    WorktreeError,
    branch_for_issue,
    default_credential_helper_command,
    ensure_worktree,
    list_worktrees,
    remove_worktree,
    worktree_path,
)

pytest.importorskip('fcntl')


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


FIXTURE_PAT = 'ghp_fixturetokenAAAAAAAAAAAAAAAAAAAAAAAA'


def _write_albedo_env(home: Path, body: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / '.env').write_text(body, encoding='utf-8')


def test_credential_helper_keeps_pat_out_of_git_config(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: `.git/config` must not contain a token-shaped string for a fixture PAT.

    Introduces FIXTURE_PAT into the system under test (via `$ALBEDO_HOME/.env`)
    and forces git to invoke the helper through `git credential fill` before
    scanning. Without that, the leak assertions would be vacuously true.
    """
    home = tmp_path / 'albedo-home'
    _write_albedo_env(home, f'GITHUB_PERSONAL_ACCESS_TOKEN={FIXTURE_PAT}\n')
    monkeypatch.setenv('ALBEDO_HOME', str(home))
    monkeypatch.delenv('GITHUB_PERSONAL_ACCESS_TOKEN', raising=False)

    wt_root = tmp_path / 'worktrees'
    info = ensure_worktree(
        repo,
        wt_root,
        'sample',
        'AI-70',
        'main',
        credential_helper_command=default_credential_helper_command(),
    )

    fill = subprocess.run(
        ['git', '-C', str(info.path), 'credential', 'fill'],
        input='protocol=https\nhost=github.com\n\n',
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert f'password={FIXTURE_PAT}' in fill.stdout

    listed = subprocess.run(
        ['git', '-C', str(info.path), 'config', '--local', '--list'],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert FIXTURE_PAT not in listed
    # Spot-check the on-disk worktree config file (newer git layout).
    git_config = repo / '.git' / 'worktrees' / info.path.name / 'config.worktree'
    if git_config.exists():
        assert FIXTURE_PAT not in git_config.read_text(encoding='utf-8')

    helper_value = subprocess.run(
        [
            'git',
            '-C',
            str(info.path),
            'config',
            '--local',
            '--get',
            f'credential.{CREDENTIAL_URL_SCOPE}.helper',
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert helper_value.startswith('!')
    assert 'albedo.git_credential' in helper_value
    assert FIXTURE_PAT not in helper_value

    username = _local_git_config(
        info.path, f'credential.{CREDENTIAL_URL_SCOPE}.username'
    )
    assert username == 'x-access-token'

    for path in info.path.rglob('*'):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        assert FIXTURE_PAT not in text, f'PAT leaked into {path}'


def test_credential_helper_replaces_prior_value_on_reuse(
    repo: Path, tmp_path: Path
) -> None:
    """Re-provisioning with a new helper command must not stack helpers."""
    wt_root = tmp_path / 'worktrees'
    info = ensure_worktree(
        repo,
        wt_root,
        'sample',
        'AI-70',
        'main',
        credential_helper_command='!/old/python -m albedo.git_credential',
    )
    ensure_worktree(
        repo,
        wt_root,
        'sample',
        'AI-70',
        'main',
        fetch=False,
        credential_helper_command='!/new/python -m albedo.git_credential',
    )
    values = subprocess.run(
        [
            'git',
            '-C',
            str(info.path),
            'config',
            '--local',
            '--get-all',
            f'credential.{CREDENTIAL_URL_SCOPE}.helper',
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert values == ['!/new/python -m albedo.git_credential']


def test_credential_helper_omitted_when_command_is_none(
    repo: Path, tmp_path: Path
) -> None:
    wt_root = tmp_path / 'worktrees'
    info = ensure_worktree(repo, wt_root, 'sample', 'AI-70', 'main')
    assert (
        _local_git_config(info.path, f'credential.{CREDENTIAL_URL_SCOPE}.helper')
        is None
    )


def test_credential_helper_resolves_pat_via_git_credential_fill(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: git invokes the helper and gets back the PAT from `.env`.

    `git credential fill` exercises the same credential-protocol path as
    `git push/fetch/pull` against an HTTPS remote — without needing an actual
    HTTP server. If git can read username/password through this channel, real
    network ops will too.
    """
    home = tmp_path / 'albedo-home'
    _write_albedo_env(home, f'GITHUB_PERSONAL_ACCESS_TOKEN={FIXTURE_PAT}\n')
    monkeypatch.setenv('ALBEDO_HOME', str(home))
    # Strip any operator PAT from the test env so the helper falls through to .env.
    monkeypatch.delenv('GITHUB_PERSONAL_ACCESS_TOKEN', raising=False)

    wt_root = tmp_path / 'worktrees'
    info = ensure_worktree(
        repo,
        wt_root,
        'sample',
        'AI-70',
        'main',
        credential_helper_command=default_credential_helper_command(),
    )

    fill = subprocess.run(
        ['git', '-C', str(info.path), 'credential', 'fill'],
        input='protocol=https\nhost=github.com\n\n',
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    out = fill.stdout
    assert 'username=x-access-token' in out
    assert f'password={FIXTURE_PAT}' in out


def test_run_git_timeout_raises(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from albedo import worktree as wt_mod

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd='git', timeout=1)

    monkeypatch.setattr(wt_mod.subprocess, 'run', fake_run)
    with pytest.raises(WorktreeError, match='timed out'):
        ensure_worktree(repo, Path('/tmp'), 'sample', 'AI-9', 'main', fetch=False)


def test_ensure_worktree_concurrent_threads_all_succeed(
    repo: Path, tmp_path: Path
) -> None:
    """Regression: 4 threads racing on the shared .git must not crash on ref locks."""
    wt_root = tmp_path / 'worktrees'
    issue_count = 4
    issues = [f'AI-{200 + i}' for i in range(issue_count)]

    def call(issue: str) -> Path:
        return ensure_worktree(repo, wt_root, 'sample', issue, 'main').path

    with ThreadPoolExecutor(max_workers=issue_count) as pool:
        paths = list(pool.map(call, issues))

    assert len(paths) == issue_count
    for path, issue in zip(paths, issues, strict=True):
        assert path.exists()
        assert path == worktree_path(wt_root, 'sample', issue)


def test_ensure_worktree_serializes_critical_section(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prove serialization: at most one thread is inside the locked block."""
    from albedo import worktree as wt_mod

    state_lock = threading.Lock()
    counters = {'inside': 0, 'max_inside': 0}
    real_repo_lock = wt_mod._repo_lock  # pyright: ignore[reportPrivateUsage]

    @contextlib.contextmanager
    def probing_repo_lock(repo_path: Path) -> Generator[None, None, None]:
        with real_repo_lock(repo_path):
            with state_lock:
                counters['inside'] += 1
                counters['max_inside'] = max(counters['max_inside'], counters['inside'])
            try:
                # Hold the section briefly so concurrent threads have a real
                # chance to overlap if serialization were broken.
                time.sleep(0.05)
                yield
            finally:
                with state_lock:
                    counters['inside'] -= 1

    monkeypatch.setattr(wt_mod, '_repo_lock', probing_repo_lock)

    wt_root = tmp_path / 'worktrees'
    issues = [f'AI-{300 + i}' for i in range(4)]

    def call(issue: str) -> None:
        ensure_worktree(repo, wt_root, 'sample', issue, 'main')

    with ThreadPoolExecutor(max_workers=len(issues)) as pool:
        list(pool.map(call, issues))

    assert counters['max_inside'] == 1, (
        f'expected serialization, saw {counters["max_inside"]} concurrent entries'
    )


def test_ensure_worktree_releases_lock_on_error(repo: Path, tmp_path: Path) -> None:
    """A failing call inside the lock must not deadlock subsequent callers."""
    wt_root = tmp_path / 'worktrees'
    with pytest.raises(WorktreeError):
        ensure_worktree(repo, wt_root, 'sample', 'AI-400', 'no-such-base', fetch=False)
    # If the lock leaked, this second call would block forever; the test
    # itself acts as the deadlock detector via pytest's per-test timeout
    # (and a follow-up call returning normally proves release).
    info = ensure_worktree(repo, wt_root, 'sample', 'AI-401', 'main')
    assert info.path.exists()


def test_remove_and_ensure_worktree_concurrent_threads_succeed(
    repo: Path, tmp_path: Path
) -> None:
    """Regression for AI-90: `remove_worktree` and `ensure_worktree` must not
    race on the shared `.git/worktrees/<name>` metadata.

    Pre-create one worktree, then concurrently remove it while a second
    `ensure_worktree` creates a different one against the same repo. Both
    operations have to complete cleanly and `list_worktrees` must reflect
    only the surviving worktree.
    """
    wt_root = tmp_path / 'worktrees'
    pre = ensure_worktree(repo, wt_root, 'sample', 'AI-500', 'main')
    assert pre.path.exists()

    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def do_remove() -> None:
        barrier.wait()
        try:
            remove_worktree(repo, pre.path)
        except BaseException as exc:
            errors.append(exc)

    def do_ensure() -> None:
        barrier.wait()
        try:
            ensure_worktree(repo, wt_root, 'sample', 'AI-501', 'main', fetch=False)
        except BaseException as exc:
            errors.append(exc)

    t_remove = threading.Thread(target=do_remove)
    t_ensure = threading.Thread(target=do_ensure)
    t_remove.start()
    t_ensure.start()
    t_remove.join(timeout=30)
    t_ensure.join(timeout=30)

    assert not t_remove.is_alive()
    assert not t_ensure.is_alive()
    assert errors == []
    assert not pre.path.exists()
    survivor = worktree_path(wt_root, 'sample', 'AI-501')
    assert survivor.exists()

    listed = list_worktrees(repo)
    branches = {w.branch for w in listed}
    assert branches == {'task/ai-501'}


def test_remove_worktree_serializes_critical_section(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`remove_worktree` and `ensure_worktree` must hold the lock mutually.

    Probe `_repo_lock` to count concurrent entries across both code paths
    and confirm the count never exceeds one.
    """
    from albedo import worktree as wt_mod

    # Pre-create worktrees so `remove_worktree` has something to do per call.
    wt_root = tmp_path / 'worktrees'
    pre_issues = [f'AI-{600 + i}' for i in range(3)]
    pre_paths = [
        ensure_worktree(repo, wt_root, 'sample', issue, 'main', fetch=False).path
        for issue in pre_issues
    ]

    state_lock = threading.Lock()
    counters = {'inside': 0, 'max_inside': 0}
    real_repo_lock = wt_mod._repo_lock  # pyright: ignore[reportPrivateUsage]

    @contextlib.contextmanager
    def probing_repo_lock(repo_path: Path) -> Generator[None, None, None]:
        with real_repo_lock(repo_path):
            with state_lock:
                counters['inside'] += 1
                counters['max_inside'] = max(counters['max_inside'], counters['inside'])
            try:
                time.sleep(0.05)
                yield
            finally:
                with state_lock:
                    counters['inside'] -= 1

    monkeypatch.setattr(wt_mod, '_repo_lock', probing_repo_lock)

    new_issues = [f'AI-{700 + i}' for i in range(3)]

    def remove_call(path: Path) -> None:
        remove_worktree(repo, path)

    def ensure_call(issue: str) -> None:
        ensure_worktree(repo, wt_root, 'sample', issue, 'main', fetch=False)

    with ThreadPoolExecutor(max_workers=len(pre_paths) + len(new_issues)) as pool:
        futures = [pool.submit(remove_call, p) for p in pre_paths]
        futures += [pool.submit(ensure_call, issue) for issue in new_issues]
        for f in futures:
            f.result()

    assert counters['max_inside'] == 1, (
        f'expected serialization, saw {counters["max_inside"]} concurrent entries'
    )


def test_remove_worktree_releases_lock_on_error(repo: Path, tmp_path: Path) -> None:
    """A `WorktreeError` from `_run_git` inside `remove_worktree` must not leak
    the lock — a follow-up `ensure_worktree` call has to acquire it without
    deadlocking. We force the failure by pointing at a path that exists but is
    not a registered worktree, which `git worktree remove` rejects.
    """
    wt_root = tmp_path / 'worktrees'
    bogus = tmp_path / 'not-a-worktree'
    bogus.mkdir()
    with pytest.raises(WorktreeError):
        remove_worktree(repo, bogus)
    # If the lock leaked, this second call would deadlock under the per-test
    # timeout; reaching it normally proves the lock was released on error.
    info = ensure_worktree(repo, wt_root, 'sample', 'AI-800', 'main')
    assert info.path.exists()
