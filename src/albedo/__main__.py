"""Albedo CLI entrypoint.

Default: poll-loop worker. `--once --issue AI-X` runs a single CODER
iteration on the given Linear issue (debugging helper).

Project selection is implicit: `albedo run` walks up from the current
directory looking for `.albedo.yaml`. To target a different repo, cd
into it. Use `albedo init-repo` to write a manifest skeleton in a new
target repo.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from albedo import __version__, paths
from albedo.config import (
    OrchestratorConfig,
    OrchestratorFileConfig,
    load_config,
    load_github_pat,
    load_linear_api_key,
    resolve_runtime_config,
)
from albedo.init_cmd import (
    init_main,
    init_repo_main,
    seed_global_home,
    seed_repo_manifest,
)
from albedo.linear_client import LinearClient
from albedo.logging_setup import configure_logging
from albedo.preflight import run_preflight
from albedo.prompt_builder import default_prompts_dir
from albedo.repo_config import (
    RepoManifest,
    RepoManifestNotFoundError,
    load_repo_manifest,
)
from albedo.setup import bootstrap as setup_bootstrap
from albedo.setup import main as setup_main
from albedo.supervisor import SupervisorOptions, supervise
from albedo.worker import RunOnceResult, run_once

_SUBCOMMANDS = frozenset({'run', 'setup', 'preflight', 'init', 'init-repo'})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='albedo',
        description=(
            'Albedo — autonomous engineering on top of Claude Code. '
            'Pulls Linear-tracked tasks for the current target repo, spawns '
            'a headless `claude -p` per task in an isolated git worktree, '
            'and opens PRs on GitHub. The default invocation runs the '
            'poll-loop supervisor with `workers` worker processes; pass '
            '--once --issue AI-X to run a single CODER iteration for '
            'debugging. The active target repo is whichever ancestor of '
            'the current directory contains a `.albedo.yaml` (use '
            '`albedo init-repo` to write one).'
        ),
        epilog=(
            'Subcommands:\n'
            '  init       Seed $ALBEDO_HOME with config, .env, prompts, '
            'mcp-servers.json (one-time per machine).\n'
            '  init-repo  Write a `.albedo.yaml` skeleton in the current '
            'directory.\n'
            '  preflight  Validate env, tools, secrets, repo + worktree '
            '(read-only).\n'
            '  setup      Bootstrap Linear team states + labels '
            '(idempotent); verifies the discovered repo`s Linear project.\n'
            '  run        (default; flag-driven) Spawn workers and start '
            'the poll loop.\n\n'
            'Paths:\n'
            '  $ALBEDO_HOME              Global config + secrets + prompts '
            '(default: ~/.config/albedo).\n'
            '  ~/.local/state/albedo/<name>            Per-project state '
            '(logs, usage.db, heartbeats).\n'
            '  ~/.local/share/albedo/worktrees/<name>  Per-project '
            'worktrees.\n\n'
            'See README.md and docs/multi-project.md for full setup.'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--version',
        action='version',
        version=f'%(prog)s {__version__}',
    )
    parser.add_argument(
        '--config',
        default=None,
        help='Path to global config.yaml (default: $ALBEDO_HOME/config.yaml).',
    )
    parser.add_argument(
        '--once',
        action='store_true',
        help='Run a single CODER iteration on the issue from --issue and exit.',
    )
    parser.add_argument(
        '--issue',
        default=None,
        help='Linear issue identifier (e.g. AI-5). Required with --once.',
    )
    parser.add_argument(
        '--agent-id',
        default='1',
        help=(
            'Agent ID for `--once` mode (single-worker debug). The supervisor '
            'mode ignores this and assigns ids 1..N from config.workers.'
        ),
    )
    parser.add_argument(
        '--workers',
        type=int,
        default=None,
        help='Override config.workers (number of worker processes to spawn).',
    )
    parser.add_argument(
        '--prompts-dir',
        default=None,
        help=('Where role prompt templates live (default: $ALBEDO_HOME/prompts).'),
    )
    parser.add_argument(
        '--mcp-config',
        default=None,
        help=(
            'MCP config JSON path passed to `claude -p --mcp-config` '
            "(default: $ALBEDO_HOME/mcp-servers.json; pass '' to disable)."
        ),
    )
    parser.add_argument(
        '--log-level',
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Python logging level (default: INFO).',
    )
    parser.add_argument(
        '--skip-stale-recovery',
        action='store_true',
        help='Do not run stale-claim recovery on startup (loop mode only).',
    )
    parser.add_argument(
        '--no-tui',
        action='store_true',
        help=(
            'Disable the live TUI even when stdout is a terminal. Set the '
            'ALBEDO_NO_TUI=1 env var for the same effect. By default '
            'the TUI auto-activates when stdout.isatty() and is skipped for '
            'piped/redirected runs.'
        ),
    )
    return parser


def _resolve_project_id(
    file_config: OrchestratorFileConfig,
    manifest: RepoManifest,
) -> str:
    """Look up the Linear project UUID for the manifest's project name."""
    api_key = load_linear_api_key()
    with LinearClient(file_config.linear.api_url, api_key) as client:
        team = client.resolve_team(file_config.linear.team)
        project = client.get_project_by_name(team.id, manifest.linear.project)
    return project.id


def _ensure_global_config_seeded(config_arg: str | None) -> bool:
    """If `$ALBEDO_HOME/config.yaml` is missing, write it and return False.

    Returns True if the config exists (or was explicitly passed via
    `--config`) — caller should proceed. Returns False if we just seeded
    the home — caller should print a hint and exit non-zero so the
    operator can edit before re-running.
    """
    if config_arg is not None:
        return True
    target = paths.global_config_path()
    if target.exists():
        return True
    home = seed_global_home(force=False)
    print(
        f'\nFirst run: seeded {home}.\n'
        f'  → Edit {home / "config.yaml"} (set linear.team) and '
        f'{home / ".env"} (LINEAR_API_KEY, GITHUB_PERSONAL_ACCESS_TOKEN), '
        'then re-run.',
        file=sys.stderr,
    )
    return False


def _auto_bootstrap_if_needed(
    file_config: OrchestratorFileConfig,
    manifest: RepoManifest,
    config: OrchestratorConfig,
) -> None:
    """Run `bootstrap()` once per (project, team) pair, gated by a marker.

    Marker lives at `<state_dir>/.bootstrapped` and contains
    `<team>\\n<project>\\n`. If contents match the current config the
    bootstrap is skipped. Failure is non-fatal — `albedo run` proceeds
    so transient Linear flakes don't block startup; the marker just
    isn't written, so the next launch retries.
    """
    marker = config.state_dir / '.bootstrapped'
    expected = f'{file_config.linear.team}\n{manifest.linear.project}\n'
    if marker.exists() and marker.read_text(encoding='utf-8') == expected:
        return
    print(
        f'albedo: bootstrapping Linear team {file_config.linear.team!r} '
        f'(project {manifest.linear.project!r}) — first run for this '
        'project. Subsequent runs skip this step.',
        file=sys.stderr,
    )
    try:
        api_key = load_linear_api_key()
        with LinearClient(file_config.linear.api_url, api_key) as client:
            report = setup_bootstrap(
                client,
                file_config.linear.team,
                project_profiles={manifest.name: manifest.linear.project},
                dry_run=False,
            )
    except Exception as exc:
        print(
            f'albedo: auto-bootstrap failed ({exc}); will retry next run.',
            file=sys.stderr,
        )
        return
    if report.has_project_failures:
        print(
            'albedo: auto-bootstrap reported project verification errors:',
            file=sys.stderr,
        )
        print(report.summary(), file=sys.stderr)
        return
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(expected, encoding='utf-8')


def _ensure_repo_manifest_seeded(start: Path) -> bool:
    """If no `.albedo.yaml` is reachable from `start`, write one in CWD.

    Returns True if a manifest was found upward from `start`. Returns
    False if we just wrote a skeleton — caller should print a hint and
    exit non-zero so the operator can edit before re-running.
    """
    try:
        load_repo_manifest(start)
    except RepoManifestNotFoundError:
        target = seed_repo_manifest(start, force=False)
        print(
            f'\nNo .albedo.yaml found upward from {start}. Wrote '
            f'{target}.\n'
            '  → Edit `name` and `linear.project` (`repo.github.*` may '
            'be auto-filled from origin), then re-run.',
            file=sys.stderr,
        )
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw and raw[0] in _SUBCOMMANDS:
        subcommand = raw[0]
        remainder = raw[1:]
        if subcommand == 'preflight':
            return _run_preflight_subcommand(remainder)
        if subcommand == 'setup':
            if not _ensure_global_config_seeded(None):
                return 2
            return setup_main(remainder)
        if subcommand == 'init':
            return init_main(remainder)
        if subcommand == 'init-repo':
            return init_repo_main(remainder)
        # 'run' falls through to the flag-driven path.
        raw = remainder

    args = build_parser().parse_args(raw)

    if args.once and not args.issue:
        print('error: --once requires --issue', file=sys.stderr)
        return 2

    if not _ensure_global_config_seeded(args.config):
        return 2
    if not _ensure_repo_manifest_seeded(Path.cwd()):
        return 2

    file_config = load_config(args.config)
    repo_root, manifest = load_repo_manifest(Path.cwd())

    project_id = _resolve_project_id(file_config, manifest)
    config: OrchestratorConfig = resolve_runtime_config(
        file_config, manifest, repo_root, project_id
    )
    project_name = config.project_name
    _auto_bootstrap_if_needed(file_config, manifest, config)

    tui_disabled_env_early = os.environ.get('ALBEDO_NO_TUI', '').strip().lower() in {
        '1',
        'true',
        'yes',
    }
    will_use_tui = (
        not args.once
        and sys.stdout.isatty()
        and not args.no_tui
        and not tui_disabled_env_early
    )
    configure_logging(
        agent_id=args.agent_id if args.once else None,
        level=args.log_level,
        state_dir=config.state_dir,
        silence_stderr=will_use_tui,
    )
    if args.workers is not None:
        if args.workers < 1:
            print('error: --workers must be >= 1', file=sys.stderr)
            return 2
        config = config.model_copy(update={'workers': args.workers})
    api_key = load_linear_api_key(agent_id=args.agent_id) if args.once else None
    github_pat = load_github_pat()
    if github_pat is None:
        print(
            'warning: GITHUB_PERSONAL_ACCESS_TOKEN not set; the GitHub MCP '
            'server will fail with Bad credentials. Add it to .env or export '
            'it in your shell.',
            file=sys.stderr,
        )
    mcp_config_path = _resolve_mcp_config_arg(args.mcp_config)
    if mcp_config_path is not None and not mcp_config_path.exists():
        print(
            f'warning: mcp-config {mcp_config_path} does not exist — '
            f'claude will be spawned without MCP servers.',
            file=sys.stderr,
        )
        mcp_config_path = None

    print(
        f'albedo: project={project_name} '
        f'(linear={config.linear.project!r}, id={project_id})',
        file=sys.stderr,
    )

    if args.once:
        assert api_key is not None
        with LinearClient(config.linear.api_url, api_key) as client:
            result = run_once(
                config=config,
                linear=client,
                issue_identifier=args.issue,
                agent_id=args.agent_id,
                prompts_dir=_resolve_prompts_dir_arg(args.prompts_dir),
                mcp_config_path=mcp_config_path,
                github_pat=github_pat,
            )
            _print_summary(result)
            return 0 if not result.claude.is_error else 1

    enable_tui = will_use_tui
    supervise(
        SupervisorOptions(
            workers=config.workers,
            config=config,
            prompts_dir=_resolve_prompts_dir_arg(args.prompts_dir),
            mcp_config_path=mcp_config_path,
            skip_stale_recovery=args.skip_stale_recovery,
            enable_tui=enable_tui,
            project_label=project_name,
        )
    )
    return 0


def _run_preflight_subcommand(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog='albedo preflight',
        description='Validate the local environment before launching workers.',
    )
    parser.add_argument(
        '--config',
        default=None,
        help='Path to global config.yaml (default: $ALBEDO_HOME/config.yaml).',
    )
    parser.add_argument(
        '--mcp-config',
        default=None,
        help=(
            'MCP config JSON path '
            "(default: $ALBEDO_HOME/mcp-servers.json; pass '' to disable)."
        ),
    )
    args = parser.parse_args(argv)
    if not _ensure_global_config_seeded(args.config):
        return 2
    if not _ensure_repo_manifest_seeded(Path.cwd()):
        return 2
    config_path = Path(args.config).expanduser() if args.config else None
    mcp_config_path = _resolve_mcp_config_arg(args.mcp_config)
    return run_preflight(config_path=config_path, mcp_config_path=mcp_config_path)


def _resolve_prompts_dir_arg(value: str | None) -> Path:
    if value is None:
        return default_prompts_dir()
    return Path(value).expanduser().absolute()


def _resolve_mcp_config_arg(value: str | None) -> Path | None:
    """None → $ALBEDO_HOME/mcp-servers.json. Empty string → disabled (None)."""
    from albedo import paths as _paths

    if value is None:
        return _paths.mcp_config_path()
    if value == '':
        return None
    return Path(value).expanduser().absolute()


def _print_summary(result: RunOnceResult) -> None:
    print(f'Issue: {result.issue.identifier} ({result.issue.state_name})')
    print(f'Role: {result.role.role}')
    print(f'Worktree: {result.worktree.path}')
    print(f'Claude exit: {result.claude.exit_code}, error={result.claude.is_error}')
    if result.claude.timed_out:
        print('TIMED OUT')
    if result.claude.usage:
        usage_summary = ', '.join(
            f'{k}={v}' for k, v in sorted(result.claude.usage.items())
        )
        print(f'Usage: {usage_summary}')
    if result.claude.result_text:
        print('--- claude result ---')
        print(result.claude.result_text)


if __name__ == '__main__':
    sys.exit(main())
