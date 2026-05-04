"""`albedo init` — seed `$ALBEDO_HOME` with bundled defaults.

Copies the prompt templates and `mcp-servers.json` shipped inside the
package into `$ALBEDO_HOME/`, writes a starter `config.yaml` and an
empty `.env` skeleton. Idempotent unless `--force` is passed: existing
files are left alone with a printed notice.

The bundled assets live under `albedo/_data/` and are read via
`importlib.resources` so the command works whether albedo was installed
as a wheel, an editable install, or run from a checkout.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Sequence
from importlib import resources
from pathlib import Path

from albedo import paths

_DATA_PACKAGE = 'albedo._data'

_CONFIG_TEMPLATE = """\
# Albedo global config — knobs shared across all target repos.
# Per-repo settings (Linear project, GitHub coords) live in `.albedo.yaml`
# at each target repo's root.

workers: 2
poll_interval_seconds: 20
poll_jitter_seconds: 5

linear:
  api_url: https://api.linear.app/graphql
  # Either a Linear team key (e.g. "ORC") or an exact team name.
  team: REPLACE_ME

usage:
  rolling_window_token_cap: 200000
  rolling_window_seconds: 18000

features:
  user_comments_in_prompt: true
  comment_redispatch: true
  comment_triage: true
  agent_body_edits: true

attachments:
  enabled: true
  max_file_mb: 10
  max_files_per_issue: 20
  allowed_extensions: [png, jpg, jpeg, gif, webp, pdf, md, txt]

models:
  coder: claude-opus-4-7
  reviewer: claude-sonnet-4-6
  architect: claude-opus-4-7
  triage: claude-sonnet-4-6
"""

_ENV_TEMPLATE = """\
# Albedo secrets. This file lives in $ALBEDO_HOME (default ~/.config/albedo).

# Required: personal API key. Settings → My account → Security & access.
LINEAR_API_KEY=lin_api_replace_me

# Optional: per-agent overrides. Each worker tries
# LINEAR_API_KEY_<AGENT_ID> first, then LINEAR_API_KEY.
# LINEAR_API_KEY_1=lin_api_agent_one
# LINEAR_API_KEY_2=lin_api_agent_two

# Required for E2E (PR creation through the GitHub MCP).
# Recommended: dedicated bot account so PRs/comments don't show your name
# and you can review/decline them on GitHub. See
# docs/per-agent-github-identities.md for setup steps.
# https://github.com/settings/tokens — scopes: repo, workflow.
GITHUB_PERSONAL_ACCESS_TOKEN=ghp_replace_me

# Optional: per-agent PAT overrides (mirrors LINEAR_API_KEY_<id>; reserved
# for the future N-bots setup, harmless if unset).
# GITHUB_PERSONAL_ACCESS_TOKEN_1=ghp_bot_one
# GITHUB_PERSONAL_ACCESS_TOKEN_2=ghp_bot_two

# Optional: bot git identity. When both are set, each worktree gets
# `git config user.name/user.email` so commits are authored by the bot
# instead of inheriting your global git config.
# GITHUB_BOT_NAME=Albedo Bot
# GITHUB_BOT_EMAIL=12345678+albedo-bot@users.noreply.github.com
# GITHUB_BOT_NAME_1=Albedo Bot 1
# GITHUB_BOT_EMAIL_1=12345678+albedo-bot-1@users.noreply.github.com
"""

_REPO_MANIFEST_TEMPLATE = """\
# Albedo per-repo manifest. The dir containing this file is the repo root.
# `albedo run` walks up from CWD to find this file.

# Project slug — used for state and worktree dir names. Keep stable.
name: REPLACE_ME

linear:
  # Exact name of the Linear project that holds issues for this repo.
  project: REPLACE_ME

repo:
  base_branch: main
  github:
    owner: REPLACE_ME
    repo: REPLACE_ME
"""


def seed_global_home(*, force: bool = False) -> Path:
    """Write `$ALBEDO_HOME/{config.yaml,.env,prompts/,mcp-servers.json}`.

    Idempotent unless `force=True`. Returns the home directory path so
    callers can print it. Used by `albedo init` and by the auto-seed
    fallback inside `albedo run`/`preflight`/`setup`.
    """
    home = paths.albedo_home()
    home.mkdir(parents=True, exist_ok=True)
    _write_text(home / 'config.yaml', _CONFIG_TEMPLATE, force=force)
    _write_text(home / '.env', _ENV_TEMPLATE, force=force)
    _seed_mcp_config(home, force=force)
    _seed_prompts(home, force=force)
    return home


def seed_repo_manifest(target_dir: Path, *, force: bool = False) -> Path:
    """Write `.albedo.yaml` in `target_dir`, prefilling github coords if possible.

    Returns the manifest path. If `git remote get-url origin` resolves
    to a GitHub URL, fills `repo.github.{owner,repo}` automatically;
    otherwise leaves placeholders. Idempotent unless `force=True`.
    """
    from albedo.repo_config import detect_github_from_git

    detected = detect_github_from_git(target_dir)
    if detected is not None:
        owner, repo = detected
        body = _REPO_MANIFEST_TEMPLATE.replace(
            '    owner: REPLACE_ME', f'    owner: {owner}'
        ).replace('    repo: REPLACE_ME', f'    repo: {repo}')
    else:
        body = _REPO_MANIFEST_TEMPLATE
    target = target_dir / '.albedo.yaml'
    _write_text(target, body, force=force)
    return target


def init_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='albedo init',
        description='Seed $ALBEDO_HOME with config, .env, prompts, mcp-servers.json.',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Overwrite files that already exist in $ALBEDO_HOME.',
    )
    args = parser.parse_args(argv)

    home = seed_global_home(force=args.force)
    print(f'albedo home: {home}', file=sys.stderr)
    print(
        'Done. Edit config.yaml and .env, then `cd` into a target repo '
        '(albedo will auto-create the per-repo manifest on first run).',
        file=sys.stderr,
    )
    return 0


def init_repo_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='albedo init-repo',
        description='Write a `.albedo.yaml` skeleton in the current directory.',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Overwrite an existing .albedo.yaml.',
    )
    args = parser.parse_args(argv)

    target = seed_repo_manifest(Path.cwd(), force=args.force)
    print(
        f'Wrote {target}. Edit `name` and `linear.project` '
        '(`repo.github.*` may already be auto-filled from git remote).',
        file=sys.stderr,
    )
    return 0


def _write_text(path: Path, content: str, *, force: bool) -> None:
    if path.exists() and not force:
        print(f'  skip {path} (exists; pass --force to overwrite)', file=sys.stderr)
        return
    path.write_text(content, encoding='utf-8')
    print(f'  wrote {path}', file=sys.stderr)


def _seed_mcp_config(home: Path, *, force: bool) -> None:
    target = home / 'mcp-servers.json'
    if target.exists() and not force:
        print(f'  skip {target} (exists; pass --force to overwrite)', file=sys.stderr)
        return
    src = resources.files(_DATA_PACKAGE).joinpath('mcp-servers.json')
    target.write_bytes(src.read_bytes())
    print(f'  wrote {target}', file=sys.stderr)


def _seed_prompts(home: Path, *, force: bool) -> None:
    target_dir = home / 'prompts'
    target_dir.mkdir(parents=True, exist_ok=True)
    src_dir = resources.files(_DATA_PACKAGE).joinpath('prompts')
    for entry in src_dir.iterdir():
        if not entry.is_file():
            continue
        dst = target_dir / entry.name
        if dst.exists() and not force:
            print(f'  skip {dst} (exists; pass --force to overwrite)', file=sys.stderr)
            continue
        # importlib.resources Traversable → use shutil-friendly read/write.
        with resources.as_file(entry) as src_path:
            shutil.copyfile(src_path, dst)
        print(f'  wrote {dst}', file=sys.stderr)
