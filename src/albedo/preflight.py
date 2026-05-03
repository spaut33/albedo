"""Pre-flight environment checks for the orchestrator.

Bundles the validations that today are scattered between `config.py`,
`__main__.py`, and runtime spawn sites. Run as
`albedo preflight` before the first launch on a new
machine — all green ⇒ exit 0; any failure ⇒ exit 1 with hints printed.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from albedo.config import (
    OrchestratorFileConfig,
    default_config_path,
    load_config,
    load_github_pat,
    load_linear_api_key,
)
from albedo.github_client import GithubClient, GithubError
from albedo.linear_client import LinearClient, LinearError

MIN_PYTHON = (3, 12)
MIN_GIT = (2, 20)


@dataclass(frozen=True, slots=True)
class CheckResult:
    ok: bool
    message: str
    hint: str = ''


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    run: Callable[[], CheckResult]


def check_python_version() -> CheckResult:
    actual = sys.version_info[:2]
    if actual >= MIN_PYTHON:
        return CheckResult(True, f'Python {actual[0]}.{actual[1]}')
    required = f'{MIN_PYTHON[0]}.{MIN_PYTHON[1]}'
    return CheckResult(
        False,
        f'Python {actual[0]}.{actual[1]} is below the required {required}',
        hint='Install Python ≥ 3.12 (e.g. via pyenv or your distro packages).',
    )


def check_git_version(
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> CheckResult:
    runner = run or _default_subprocess_run
    try:
        result = runner(
            ['git', '--version'],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return CheckResult(
            False,
            'git not found on PATH',
            hint='Install git ≥ 2.20 (worktree support).',
        )
    if result.returncode != 0:
        return CheckResult(
            False,
            f'git --version exited {result.returncode}',
            hint=(result.stderr or '').strip() or 'Reinstall git.',
        )
    parsed = _parse_git_version(result.stdout)
    if parsed is None:
        return CheckResult(
            False,
            f'could not parse git version from {result.stdout.strip()!r}',
            hint='Upgrade git to a recent stable release.',
        )
    if parsed < MIN_GIT:
        return CheckResult(
            False,
            f'git {parsed[0]}.{parsed[1]} is below required {MIN_GIT[0]}.{MIN_GIT[1]}',
            hint='Upgrade git — albedo relies on `git worktree`.',
        )
    return CheckResult(True, f'git {parsed[0]}.{parsed[1]}')


def check_claude_binary(
    which: Callable[[str], str | None] | None = None,
) -> CheckResult:
    locator = which or shutil.which
    path = locator('claude')
    if path is None:
        return CheckResult(
            False,
            'claude binary not found on PATH',
            hint='Install the Claude Code CLI (https://docs.claude.com/en/docs/claude-code).',
        )
    return CheckResult(True, f'claude at {path}')


def check_config_loads(config_path: Path | None = None) -> CheckResult:
    target = config_path or default_config_path()
    if not target.exists():
        return CheckResult(
            False,
            f'config file {target} not found',
            hint='Run `albedo init` to seed $ALBEDO_HOME with config + prompts.',
        )
    try:
        cfg = load_config(target)
    except Exception as exc:
        return CheckResult(
            False,
            f'config {target} failed to parse',
            hint=str(exc),
        )
    _ = cfg  # parsed; further checks happen in check_repo_and_worktree
    return CheckResult(True, f'config {target} parsed')


def check_repo_and_worktree(
    config: OrchestratorFileConfig,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    cwd: Path | None = None,
) -> CheckResult:
    """Verify the discovered target repo and that its worktree dir is writable.

    Walks up from `cwd` (default: current working directory) for
    `.albedo.yaml`. If the manifest is missing, reports it as a failure
    with a hint to run `albedo init-repo`.
    """
    from albedo.repo_config import RepoManifestNotFoundError, load_repo_manifest

    runner = run or _default_subprocess_run
    bad: list[str] = []
    start = cwd or Path.cwd()
    try:
        repo_root, manifest = load_repo_manifest(start)
    except RepoManifestNotFoundError as exc:
        return CheckResult(False, 'no .albedo.yaml found', hint=str(exc))

    if not repo_root.exists():
        bad.append(f'{manifest.name}: repo path {repo_root} does not exist')
    else:
        try:
            result = runner(
                ['git', '-C', str(repo_root), 'rev-parse', '--git-dir'],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            bad.append(f'{manifest.name}: git binary unavailable for repo check')
        else:
            if result.returncode != 0:
                bad.append(f'{manifest.name}: {repo_root} is not a git repo')

    from albedo import paths as _paths

    worktree_root = (
        (config.worktree_root / manifest.name)
        if config.worktree_root is not None
        else _paths.worktree_home(manifest.name)
    )
    try:
        worktree_root.mkdir(parents=True, exist_ok=True)
        probe = worktree_root / '.preflight-probe'
        probe.write_text('ok', encoding='utf-8')
        probe.unlink()
    except OSError as exc:
        bad.append(f'worktree_root {worktree_root} not writable: {exc}')
    if bad:
        return CheckResult(
            False,
            'repo / worktree checks failed',
            hint='; '.join(bad),
        )
    return CheckResult(
        True,
        f'repo {manifest.name} at {repo_root}; worktree_root {worktree_root} writable',
    )


def check_mcp_config(path: Path) -> CheckResult:
    if path.exists():
        return CheckResult(True, f'MCP config at {path}')
    return CheckResult(
        False,
        f'MCP config {path} not found',
        hint='Run `albedo init` to seed it, or pass --mcp-config "" to disable MCP.',
    )


def check_linear_api(
    *,
    env: dict[str, str] | None = None,
    client_factory: Callable[..., LinearClient] | None = None,
    api_url: str = 'https://api.linear.app/graphql',
) -> CheckResult:
    try:
        secret = load_linear_api_key(env) if env is not None else load_linear_api_key()
    except RuntimeError as exc:
        return CheckResult(False, 'LINEAR_API_KEY missing', hint=str(exc))
    factory = client_factory or LinearClient
    try:
        with factory(api_url, secret) as client:
            viewer = client.viewer()
    except LinearError as exc:
        return CheckResult(
            False,
            'Linear API rejected the token',
            hint=str(exc),
        )
    except Exception as exc:
        return CheckResult(
            False,
            'Linear API call failed',
            hint=str(exc),
        )
    return CheckResult(True, f'Linear viewer={viewer.name or viewer.id}')


def check_github_pat(
    *,
    client_factory: Callable[..., GithubClient] | None = None,
) -> CheckResult:
    try:
        pat = load_github_pat(required=True)
    except RuntimeError as exc:
        return CheckResult(
            False,
            'GITHUB_PERSONAL_ACCESS_TOKEN missing',
            hint=str(exc),
        )
    assert pat is not None
    factory = client_factory or GithubClient
    try:
        with factory(pat) as client:
            login = client.get_authenticated_login()
    except GithubError as exc:
        return CheckResult(False, 'GitHub PAT rejected', hint=str(exc))
    except Exception as exc:
        return CheckResult(False, 'GitHub API call failed', hint=str(exc))
    return CheckResult(True, f'GitHub user={login}')


def build_checks(
    *,
    config_path: Path | None = None,
    mcp_config_path: Path | None = None,
) -> list[Check]:
    """Assemble the standard check sequence in display order.

    `config`-dependent checks lazily re-load the file so a failure in
    `check_config_loads` does not crash later checks; they degrade to
    'skipped — config invalid' instead.
    """
    config_holder: dict[str, OrchestratorFileConfig] = {}

    def _config_check() -> CheckResult:
        result = check_config_loads(config_path)
        if result.ok:
            with contextlib.suppress(Exception):
                config_holder['cfg'] = load_config(config_path or default_config_path())
        return result

    def _repo_check() -> CheckResult:
        cfg = config_holder.get('cfg')
        if cfg is None:
            return CheckResult(
                False,
                'skipped — config did not load',
                hint='Fix the config check above first.',
            )
        return check_repo_and_worktree(cfg)

    def _linear_check() -> CheckResult:
        cfg = config_holder.get('cfg')
        api_url = (
            cfg.linear.api_url if cfg is not None else 'https://api.linear.app/graphql'
        )
        return check_linear_api(api_url=api_url)

    def _mcp_check() -> CheckResult:
        from albedo import paths as _paths

        target = mcp_config_path or _paths.mcp_config_path()
        return check_mcp_config(target)

    return [
        Check('Python version', check_python_version),
        Check('git version', check_git_version),
        Check('claude binary', check_claude_binary),
        Check('config file', _config_check),
        Check('repo + worktree', _repo_check),
        Check('MCP config', _mcp_check),
        Check('Linear API', _linear_check),
        Check('GitHub PAT', check_github_pat),
    ]


def render_report(
    results: Iterable[tuple[Check, CheckResult]],
    *,
    console: Console | None = None,
) -> None:
    output = console or Console()
    table = Table(title='albedo preflight', show_lines=False)
    table.add_column('check', no_wrap=True)
    table.add_column('status', no_wrap=True)
    table.add_column('detail')
    for check, result in results:
        status = '[green]✓[/green]' if result.ok else '[red]✗[/red]'
        detail = result.message
        if not result.ok and result.hint:
            detail = f'{detail}\n[dim]hint: {result.hint}[/dim]'
        table.add_row(check.name, status, detail)
    output.print(table)


def run_preflight(
    *,
    config_path: Path | None = None,
    mcp_config_path: Path | None = None,
    console: Console | None = None,
) -> int:
    checks = build_checks(config_path=config_path, mcp_config_path=mcp_config_path)
    results: list[tuple[Check, CheckResult]] = []
    for check in checks:
        try:
            result = check.run()
        except Exception as exc:
            result = CheckResult(False, f'{check.name} raised', hint=str(exc))
        results.append((check, result))
    render_report(results, console=console)
    return 0 if all(r.ok for _, r in results) else 1


def _parse_git_version(text: str) -> tuple[int, int] | None:
    match = re.search(r'(\d+)\.(\d+)', text)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _default_subprocess_run(
    args: list[str],
    **kwargs: object,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, **kwargs)  # type: ignore[arg-type]
