"""Typed configuration loaded from `$ALBEDO_HOME/config.yaml`.

The global config carries shared runtime knobs (workers, polling, usage,
features, models, attachments) and a `linear.team` binding. Per-target-repo
settings (Linear project name, repo base branch, GitHub coords) live in a
`.albedo.yaml` at the target repo's root and are loaded by
`albedo.repo_config`.

Workers and the supervisor consume `OrchestratorConfig` — a runtime view
produced by `resolve_runtime_config(file_config, manifest, project_id)`
that combines the global config with the discovered repo manifest. State
and worktree dirs are derived from `paths.state_home(name)` and
`paths.worktree_home(name)` so two `albedo run` invocations against
different repos never collide.

The Linear API token is read from `$ALBEDO_HOME/.env` (or process env),
never from YAML.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from albedo import paths

if TYPE_CHECKING:
    from albedo.repo_config import RepoManifest

LINEAR_API_KEY_ENV = 'LINEAR_API_KEY'
GITHUB_PAT_ENV = 'GITHUB_PERSONAL_ACCESS_TOKEN'
GITHUB_BOT_NAME_ENV = 'GITHUB_BOT_NAME'
GITHUB_BOT_EMAIL_ENV = 'GITHUB_BOT_EMAIL'

# `albedo init` and `albedo init-repo` write this sentinel into every field
# the operator must edit before running. We reject it loudly at config load
# rather than letting Linear/GitHub return a confusing 404.
PLACEHOLDER_SENTINEL = 'REPLACE_ME'


def reject_placeholder(value: str, *, field: str) -> str:
    if value.strip() == PLACEHOLDER_SENTINEL:
        raise ValueError(
            f'{field} is still {PLACEHOLDER_SENTINEL!r} — edit '
            f'{paths.global_config_path()} (for global fields) or the repo '
            f'`.albedo.yaml` (for per-repo fields) before running albedo.'
        )
    return value


def default_config_path() -> Path:
    return paths.global_config_path()


def default_env_file() -> Path:
    return paths.env_file_path()


class GithubConfig(BaseModel):
    owner: str = Field(description='GitHub owner (user or org), e.g. "octocat"')
    repo: str = Field(description='GitHub repo name, e.g. "sample"')

    @property
    def slug(self) -> str:
        return f'{self.owner}/{self.repo}'

    @field_validator('owner', 'repo', mode='after')
    @classmethod
    def _no_placeholder(cls, value: str, info: Any) -> str:
        return reject_placeholder(value, field=f'repo.github.{info.field_name}')


class RepoConfig(BaseModel):
    path: Path = Field(description='Local clone path of the target repo')
    base_branch: str = Field(default='main')
    github: GithubConfig | None = Field(
        default=None,
        description='GitHub coordinates for the target repo. Required for E2E.',
    )

    @field_validator('path', mode='after')
    @classmethod
    def _absolute_path(cls, value: Path) -> Path:
        return value.expanduser().absolute()


class UsageConfig(BaseModel):
    rolling_window_token_cap: int = Field(
        default=200_000,
        description='Hard cap on input+output tokens within a rolling 5h window',
    )
    rolling_window_seconds: int = Field(default=5 * 60 * 60)


class DispatchConfig(BaseModel):
    """Single-poller fan-out tunables.

    The supervisor polls Linear once per tick and offers candidates to
    workers via an `mp.Queue`. These knobs size the queue and bound
    how long a stale offer can occupy the in-flight set.
    """

    queue_size_per_worker: int = Field(
        default=2,
        ge=1,
        description='Dispatch queue size = workers * this; cap on burst offers.',
    )
    offer_ttl_seconds: int = Field(
        default=300,
        ge=1,
        description='Drop in-flight entry after this; the next tick may re-offer.',
    )
    poll_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        description='Worker queue.get timeout — bound on shutdown latency.',
    )


class LinearConfig(BaseModel):
    """Runtime Linear settings consumed by workers and housekeeping.

    `team` is required; `project` and `project_id` are populated by
    `resolve_runtime_config` after we look up the chosen profile's project
    on Linear. They stay optional so unit tests that build a config
    directly don't have to fake the resolve step.
    """

    api_url: str = Field(default='https://api.linear.app/graphql')
    team: str = Field(
        description='Linear team key (e.g. "ORC") or exact team name (e.g. "AI-Team")',
        min_length=1,
    )
    project: str | None = Field(
        default=None,
        description='Active project name, set during runtime resolution.',
    )
    project_id: str | None = Field(
        default=None,
        description='Active Linear project UUID, resolved from `project`.',
    )

    @field_validator('team', mode='after')
    @classmethod
    def _team_no_placeholder(cls, value: str) -> str:
        return reject_placeholder(value, field='linear.team')


class ModelsConfig(BaseModel):
    """Per-role Claude model selection.

    Each field maps to a `claude -p --model <id>` value used when spawning
    that role. `None` (the default) falls back to whatever model the
    `claude` CLI is configured with — preserving prior behaviour for
    operators who don't opt in.

    `triage` is used by the cheap YES/NO comment-redispatch gate
    (`comment_triage`); it pairs naturally with a small/fast model.
    """

    coder: str | None = Field(default=None)
    reviewer: str | None = Field(default=None)
    architect: str | None = Field(default=None)
    triage: str | None = Field(default=None)

    def for_role(self, role: str) -> str | None:
        return {
            'CODER': self.coder,
            'REVIEWER': self.reviewer,
            'ARCHITECT': self.architect,
        }.get(role)


class RoleExtraToolsConfig(BaseModel):
    """Operator-level allow-list extensions appended to each role's
    built-in tools at spawn time.

    The role-defining tools (Read/Edit/Bash/Skill/...) live in `dispatch.py`
    because they encode what the role *is*. External integrations (any
    `mcp__<server>__*` the operator has wired up — `github` proxy,
    `context7` docs, custom servers, etc.) are configuration, not code:
    adding a new MCP should not require a fork.

    Defaults bundle the GitHub MCP so a fresh install can open PRs out of
    the box. Operators who want to turn it off (or grant additional MCPs
    like `mcp__context7__*`) override the per-role tuple in `config.yaml`.
    """

    coder_allow_extra: tuple[str, ...] = Field(default=('mcp__github__*',))
    reviewer_allow_extra: tuple[str, ...] = Field(default=('mcp__github__*',))
    architect_allow_extra: tuple[str, ...] = Field(default=())

    def for_role(self, role: str) -> tuple[str, ...]:
        return {
            'CODER': self.coder_allow_extra,
            'REVIEWER': self.reviewer_allow_extra,
            'ARCHITECT': self.architect_allow_extra,
        }.get(role, ())


class AttachmentsConfig(BaseModel):
    """Linear → worktree attachment download knobs.

    Covers both Attachments-API entries and `uploads.linear.app` URLs
    embedded in markdown bodies. Files land in
    `<worktree>/.linear-attachments/<issue>/` and are referenced by
    relative path in the agent prompt; the agent picks them up via Read.
    """

    enabled: bool = Field(default=True)
    max_file_mb: int = Field(default=10, ge=1)
    max_files_per_issue: int = Field(default=20, ge=1)
    allowed_extensions: tuple[str, ...] = Field(
        default=('png', 'jpg', 'jpeg', 'gif', 'webp', 'pdf', 'md', 'txt'),
        description='Lowercase, no leading dot. Files outside this set are skipped.',
    )


class FeaturesConfig(BaseModel):
    """Toggles for the user-comment feedback loop. All default on."""

    user_comments_in_prompt: bool = Field(
        default=True,
        description='Inject filtered user comments into every role prompt.',
    )
    comment_redispatch: bool = Field(
        default=True,
        description='Re-dispatch issues parked in human-gated states when a new '
        'user comment lands.',
    )
    ci_failed_pr_dispatch: bool = Field(
        default=True,
        description='Re-route Awaiting-approval issues whose PR CI has freshly '
        'failed back to In Progress, with a comment carrying the failing log tail.',
    )
    comment_triage: bool = Field(
        default=True,
        description='Before redispatching on a new user comment, ask the LLM '
        'whether the reply is substantive enough to act on. Skip the fire on '
        'NO so non-answers ("ok", "let me think") do not flip state.',
    )
    agent_body_edits: bool = Field(
        default=True,
        description='Honour <<<ISSUE_UPDATE>>> markers in agent output.',
    )


class _SharedRuntimeFields(BaseModel):
    """Fields shared between the file-level and runtime config models.

    State and worktree dirs default to `None` here; they are derived per
    project from `paths.state_home(name)` / `paths.worktree_home(name)`
    by `resolve_runtime_config`. Operators may still pin them in the
    global YAML for a custom layout.
    """

    workers: int = Field(default=2, ge=1, le=16)
    poll_interval_seconds: int = Field(default=20, ge=5)
    poll_jitter_seconds: int = Field(default=5, ge=0)
    state_dir: Path | None = Field(
        default=None,
        description='Override for the per-project state dir root.',
    )
    worktree_root: Path | None = Field(
        default=None,
        description='Override for the per-project worktree root.',
    )
    archive_done_after_days: int = Field(
        default=7,
        ge=0,
        description='Archive Done issues older than this. 0 disables the job.',
    )
    max_attempts_before_escalation: int = Field(
        default=3,
        ge=1,
        description='Reviewer REQUEST_CHANGES count after which the issue is '
        'parked in Awaiting approval with the `stuck` label.',
    )
    linear: LinearConfig
    usage: UsageConfig = Field(default_factory=UsageConfig)
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    tools: RoleExtraToolsConfig = Field(default_factory=RoleExtraToolsConfig)
    attachments: AttachmentsConfig = Field(default_factory=AttachmentsConfig)

    @field_validator('state_dir', 'worktree_root', mode='after')
    @classmethod
    def _expand(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        return value.expanduser().absolute()


class OrchestratorFileConfig(_SharedRuntimeFields):
    """Schema of `$ALBEDO_HOME/config.yaml` after parsing.

    Carries only globals shared across every target repo. Per-repo
    settings (Linear project, GitHub coords) live in `.albedo.yaml` at
    each repo's root and are loaded by `albedo.repo_config`.
    """


class OrchestratorConfig(_SharedRuntimeFields):
    """Runtime view fed to the supervisor and worker loop.

    Built by `resolve_runtime_config` from the global config plus a
    discovered `RepoManifest`. `state_dir` and `worktree_root` are
    populated (never None) and scoped under the manifest's `name`.
    """

    project_name: str = Field(min_length=1)
    repo: RepoConfig
    state_dir: Path = Field(...)  # type: ignore[assignment]
    worktree_root: Path = Field(...)  # type: ignore[assignment]


def load_config(path: Path | str | None = None) -> OrchestratorFileConfig:
    """Load and validate orchestrator config from YAML.

    Raises pydantic.ValidationError on missing/invalid fields. Loud failure is
    deliberate — a misconfigured orchestrator should not silently start.
    """
    config_path = Path(path) if path is not None else default_config_path()
    raw: Any = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    if not isinstance(raw, dict):
        raise ValueError(f'Config root must be a mapping, got {type(raw).__name__}')
    return OrchestratorFileConfig.model_validate(raw)


def resolve_runtime_config(
    file_config: OrchestratorFileConfig,
    manifest: RepoManifest,
    repo_root: Path,
    project_id: str,
) -> OrchestratorConfig:
    """Combine global config + discovered repo manifest into a runtime view.

    `state_dir` and `worktree_root` come from the global override if
    operators pinned them in `config.yaml`, otherwise from
    `paths.state_home(name)` / `paths.worktree_home(name)`. Either way
    they are scoped under `manifest.name` so two `albedo run` invocations
    against different repos never collide.
    """
    name = manifest.name
    state_dir = (
        (file_config.state_dir / name)
        if file_config.state_dir is not None
        else paths.state_home(name)
    )
    worktree_root = (
        (file_config.worktree_root / name)
        if file_config.worktree_root is not None
        else paths.worktree_home(name)
    )
    runtime_linear = LinearConfig(
        api_url=file_config.linear.api_url,
        team=file_config.linear.team,
        project=manifest.linear.project,
        project_id=project_id,
    )
    return OrchestratorConfig(
        workers=file_config.workers,
        poll_interval_seconds=file_config.poll_interval_seconds,
        poll_jitter_seconds=file_config.poll_jitter_seconds,
        state_dir=state_dir,
        worktree_root=worktree_root,
        archive_done_after_days=file_config.archive_done_after_days,
        max_attempts_before_escalation=file_config.max_attempts_before_escalation,
        linear=runtime_linear,
        usage=file_config.usage,
        dispatch=file_config.dispatch,
        features=file_config.features,
        models=file_config.models,
        tools=file_config.tools,
        attachments=file_config.attachments,
        project_name=name,
        repo=RepoConfig(
            path=repo_root,
            base_branch=manifest.repo.base_branch,
            github=manifest.repo.github,
        ),
    )


class _Secrets(BaseSettings):
    """Internal pydantic-settings model used to read LINEAR_API_KEY and PAT.

    Reads from process env first, then from a `.env` file. Callers pass the
    `.env` path via `_env_file=`; the default resolved by callers is
    `paths.env_file_path()` (`$ALBEDO_HOME/.env`).
    """

    linear_api_key: SecretStr = Field(default=SecretStr(''))
    github_personal_access_token: SecretStr = Field(default=SecretStr(''))

    model_config = SettingsConfigDict(
        env_file_encoding='utf-8',
        extra='ignore',
    )


def load_linear_api_key(
    env: dict[str, str] | None = None,
    *,
    env_file: Path | str | None = None,
    agent_id: str | None = None,
) -> SecretStr:
    """Resolve the Linear API token, optionally agent-specific.

    Resolution order:
      1. `LINEAR_API_KEY_<AGENT_ID>` if `agent_id` is given (uppercased,
         non-alphanumeric replaced by `_`).
      2. `LINEAR_API_KEY` (the shared default).
    Each level checks: explicit `env` mapping → process env → `.env` file.
    Raises `RuntimeError` with a helpful message if neither source resolves.
    """
    keys = _candidate_env_keys(agent_id, base=LINEAR_API_KEY_ENV)

    if env is not None:
        for key in keys:
            raw = env.get(key, '').strip()
            if raw:
                return SecretStr(raw)
        raise RuntimeError(_missing_message(agent_id, base=LINEAR_API_KEY_ENV))

    config_kwargs: dict[str, Any] = {
        '_env_file': str(env_file if env_file is not None else default_env_file()),
    }

    if agent_id is None:
        secrets = _Secrets(**config_kwargs)
        value = secrets.linear_api_key.get_secret_value().strip()
        if value:
            return SecretStr(value)
        raise RuntimeError(_missing_message(None, base=LINEAR_API_KEY_ENV))

    # Agent-specific lookup uses dynamic env-var names that pydantic-settings
    # doesn't know about ahead of time, so do a manual resolve.
    resolved = _resolve_dynamic_env(keys, env_file=env_file)
    if resolved is not None:
        return SecretStr(resolved)
    raise RuntimeError(_missing_message(agent_id, base=LINEAR_API_KEY_ENV))


def _candidate_env_keys(agent_id: str | None, *, base: str) -> list[str]:
    if agent_id is None:
        return [base]
    sanitized = ''.join(c if c.isalnum() else '_' for c in agent_id).upper()
    return [f'{base}_{sanitized}', base]


def _env_file_overlay(env_file: Path | str | None) -> dict[str, str]:
    """Read `.env` into a dict for ad-hoc lookups.

    Used only when we need agent-specific keys that aren't part of the
    static `_Secrets` schema. Lines starting with `#` and blank lines are
    skipped. Quotes are not interpreted.
    """
    path = Path(env_file) if env_file is not None else default_env_file()
    if not path.exists():
        return {}
    overlay: dict[str, str] = {}
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        name, _, value = line.partition('=')
        overlay[name.strip()] = value.strip().strip('"').strip("'")
    return overlay


def _resolve_dynamic_env(keys: list[str], *, env_file: Path | str | None) -> str | None:
    """Look up env-var names dynamically (process env first, then `.env`)."""
    import os

    overlay = _env_file_overlay(env_file)
    for key in keys:
        raw = (os.environ.get(key) or overlay.get(key) or '').strip()
        if raw:
            return raw
    return None


def _missing_message(agent_id: str | None, *, base: str) -> str:
    if agent_id is None:
        target = base
    else:
        sanitized = ''.join(c if c.isalnum() else '_' for c in agent_id).upper()
        target = f'{base}_{sanitized} (or {base} as fallback)'
    return (
        f'{target} is not set. Put it in {default_env_file()} '
        f'(run `albedo init` to seed it) or export it in your shell.'
    )


def load_github_pat(
    env: dict[str, str] | None = None,
    *,
    env_file: Path | str | None = None,
    agent_id: str | None = None,
    required: bool = False,
) -> SecretStr | None:
    """Resolve the GitHub PAT used by the GitHub MCP server.

    Resolution mirrors `load_linear_api_key`: when `agent_id` is given, try
    `GITHUB_PERSONAL_ACCESS_TOKEN_<AGENT_ID>` first, then fall back to the
    shared `GITHUB_PERSONAL_ACCESS_TOKEN`. Each level checks explicit `env`
    → process env → `.env` file.

    Returns None if the variable is unset and `required` is False (the GitHub
    MCP just won't be available — useful for unit tests). Raises when
    `required` is True and no source resolves.
    """
    keys = _candidate_env_keys(agent_id, base=GITHUB_PAT_ENV)

    if env is not None:
        for key in keys:
            raw = env.get(key, '').strip()
            if raw:
                return SecretStr(raw)
        if required:
            raise RuntimeError(_missing_message(agent_id, base=GITHUB_PAT_ENV))
        return None

    config_kwargs: dict[str, Any] = {
        '_env_file': str(env_file if env_file is not None else default_env_file()),
    }

    if agent_id is None:
        secrets = _Secrets(**config_kwargs)
        value = secrets.github_personal_access_token.get_secret_value().strip()
        if value:
            return SecretStr(value)
        if required:
            raise RuntimeError(_missing_message(None, base=GITHUB_PAT_ENV))
        return None

    resolved = _resolve_dynamic_env(keys, env_file=env_file)
    if resolved is not None:
        return SecretStr(resolved)
    if required:
        raise RuntimeError(_missing_message(agent_id, base=GITHUB_PAT_ENV))
    return None


def load_bot_identity(
    env: dict[str, str] | None = None,
    *,
    env_file: Path | str | None = None,
    agent_id: str | None = None,
) -> tuple[str, str] | None:
    """Resolve the bot's git author identity (name, email).

    Used by `worktree.ensure_worktree` to set per-worktree
    `git config user.name/user.email` so commits are attributed to the bot
    rather than the operator's global git config.

    Both `GITHUB_BOT_NAME` and `GITHUB_BOT_EMAIL` must resolve — otherwise
    returns `None` and the worktree falls through to the operator's global
    git config (current behaviour). Per-agent suffixes mirror the per-agent
    PAT pattern (`GITHUB_BOT_NAME_<AGENT_ID>`, `GITHUB_BOT_EMAIL_<AGENT_ID>`).
    """
    name_keys = _candidate_env_keys(agent_id, base=GITHUB_BOT_NAME_ENV)
    email_keys = _candidate_env_keys(agent_id, base=GITHUB_BOT_EMAIL_ENV)

    if env is not None:
        name = _first_match(env, name_keys)
        email = _first_match(env, email_keys)
    else:
        name = _resolve_dynamic_env(name_keys, env_file=env_file)
        email = _resolve_dynamic_env(email_keys, env_file=env_file)

    if not name or not email:
        return None
    return (name, email)


def _first_match(env: dict[str, str], keys: list[str]) -> str | None:
    for key in keys:
        raw = env.get(key, '').strip()
        if raw:
            return raw
    return None
