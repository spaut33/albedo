"""XDG-anchored default paths for albedo.

Single source of truth for where global config, secrets, prompt templates,
the MCP config, per-project state, and worktrees live on disk. Resolution
order honours the XDG Base Directory spec with `$ALBEDO_HOME` as the
top-level override:

    config + secrets + prompts + mcp-servers.json:
        $ALBEDO_HOME → $XDG_CONFIG_HOME/albedo → ~/.config/albedo

    per-project state (logs, usage.db, heartbeats, claims, queues):
        $XDG_STATE_HOME/albedo/<name> → ~/.local/state/albedo/<name>

    per-project worktrees:
        $XDG_DATA_HOME/albedo/worktrees/<name> →
        ~/.local/share/albedo/worktrees/<name>

Nothing here touches the filesystem; callers create the directories they
need. Functions return absolute, expanded paths.
"""

from __future__ import annotations

import os
from pathlib import Path

ALBEDO_HOME_ENV = 'ALBEDO_HOME'
XDG_CONFIG_HOME_ENV = 'XDG_CONFIG_HOME'
XDG_STATE_HOME_ENV = 'XDG_STATE_HOME'
XDG_DATA_HOME_ENV = 'XDG_DATA_HOME'


def _resolve_xdg(env_var: str, default_relative: str) -> Path:
    raw = os.environ.get(env_var, '').strip()
    if raw:
        return Path(raw).expanduser().absolute()
    return (Path.home() / default_relative).absolute()


def albedo_home() -> Path:
    """Root directory for global config, secrets, prompts, mcp-servers.json."""
    raw = os.environ.get(ALBEDO_HOME_ENV, '').strip()
    if raw:
        return Path(raw).expanduser().absolute()
    return _resolve_xdg(XDG_CONFIG_HOME_ENV, '.config') / 'albedo'


def state_home(name: str) -> Path:
    """Per-project state dir: logs, usage.db, heartbeats, claims, queues."""
    return _resolve_xdg(XDG_STATE_HOME_ENV, '.local/state') / 'albedo' / name


def worktree_home(name: str) -> Path:
    """Per-project worktree root used by `git worktree add`."""
    base = _resolve_xdg(XDG_DATA_HOME_ENV, '.local/share')
    return base / 'albedo' / 'worktrees' / name


def global_config_path() -> Path:
    return albedo_home() / 'config.yaml'


def env_file_path() -> Path:
    return albedo_home() / '.env'


def prompts_dir() -> Path:
    return albedo_home() / 'prompts'


def mcp_config_path() -> Path:
    return albedo_home() / 'mcp-servers.json'
