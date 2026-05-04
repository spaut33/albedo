"""Tests for the YAML config loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from albedo.config import (
    GITHUB_BOT_EMAIL_ENV,
    GITHUB_BOT_NAME_ENV,
    GITHUB_PAT_ENV,
    LINEAR_API_KEY_ENV,
    LinearConfig,
    OrchestratorFileConfig,
    UsageConfig,
    default_config_path,
    load_bot_identity,
    load_config,
    load_github_pat,
    load_linear_api_key,
    resolve_runtime_config,
)
from albedo.repo_config import RepoManifest, RepoManifestLinear, RepoManifestRepo

_MIN_GLOBAL = 'linear:\n  team: ORC\n'


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / 'config.yaml'
    p.write_text(body, encoding='utf-8')
    return p


def _isolate_secrets_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point ALBEDO_HOME at an empty tmp dir so _Secrets reads no .env."""
    monkeypatch.setenv('ALBEDO_HOME', str(tmp_path))
    monkeypatch.delenv(LINEAR_API_KEY_ENV, raising=False)
    monkeypatch.delenv(GITHUB_PAT_ENV, raising=False)
    monkeypatch.delenv(GITHUB_BOT_NAME_ENV, raising=False)
    monkeypatch.delenv(GITHUB_BOT_EMAIL_ENV, raising=False)


def _make_manifest(name: str = 'sample', project: str = 'Sample') -> RepoManifest:
    return RepoManifest(
        name=name,
        linear=RepoManifestLinear(project=project),
        repo=RepoManifestRepo(),
    )


def test_default_config_path_uses_albedo_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv('ALBEDO_HOME', str(tmp_path))
    assert default_config_path() == tmp_path / 'config.yaml'


def test_load_minimal_config(tmp_path: Path) -> None:
    cfg_path = _write(tmp_path, 'workers: 3\nlinear:\n  team: ORC\n')
    cfg = load_config(cfg_path)
    assert isinstance(cfg, OrchestratorFileConfig)
    assert cfg.workers == 3
    assert cfg.linear == LinearConfig(team='ORC')
    assert cfg.usage == UsageConfig()


def test_load_full_config(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        """
        workers: 4
        poll_interval_seconds: 30
        poll_jitter_seconds: 0
        state_dir: ~/.albedo/state
        worktree_root: ~/.albedo/worktrees
        linear:
          api_url: https://api.linear.app/graphql
          team: TEAM
        usage:
          rolling_window_token_cap: 50000
          rolling_window_seconds: 3600
        """,
    )
    cfg = load_config(cfg_path)
    assert cfg.workers == 4
    assert cfg.poll_interval_seconds == 30
    assert cfg.poll_jitter_seconds == 0
    assert cfg.state_dir is not None
    assert cfg.state_dir.is_absolute()
    assert cfg.worktree_root is not None
    assert cfg.worktree_root.is_absolute()
    assert cfg.linear.team == 'TEAM'
    assert cfg.usage.rolling_window_token_cap == 50_000
    assert cfg.usage.rolling_window_seconds == 3600


def test_load_rejects_non_mapping_root(tmp_path: Path) -> None:
    cfg_path = _write(tmp_path, '- 1\n- 2\n')
    with pytest.raises(ValueError, match='must be a mapping'):
        load_config(cfg_path)


def test_load_rejects_invalid_workers(tmp_path: Path) -> None:
    cfg_path = _write(tmp_path, 'workers: 0\nlinear:\n  team: ORC\n')
    with pytest.raises(ValidationError):
        load_config(cfg_path)


def test_load_rejects_placeholder_team(tmp_path: Path) -> None:
    cfg_path = _write(tmp_path, 'workers: 1\nlinear:\n  team: REPLACE_ME\n')
    with pytest.raises(ValidationError, match='REPLACE_ME'):
        load_config(cfg_path)


def test_load_rejects_missing_linear(tmp_path: Path) -> None:
    cfg_path = _write(tmp_path, 'workers: 2\n')
    with pytest.raises(ValidationError):
        load_config(cfg_path)


def test_load_uses_default_path_when_none_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('ALBEDO_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text(
        'workers: 1\n' + _MIN_GLOBAL, encoding='utf-8'
    )
    cfg = load_config()
    assert cfg.workers == 1


def test_resolve_runtime_config_uses_xdg_state_when_global_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    cfg_path = _write(tmp_path, _MIN_GLOBAL)
    cfg = load_config(cfg_path)
    runtime = resolve_runtime_config(
        cfg, _make_manifest('beta', 'Beta'), Path('/tmp/beta'), 'prj-uuid'
    )
    assert runtime.project_name == 'beta'
    assert runtime.repo.path == Path('/tmp/beta').absolute()
    assert runtime.linear.project == 'Beta'
    assert runtime.linear.project_id == 'prj-uuid'
    assert runtime.linear.team == 'ORC'
    assert runtime.state_dir == tmp_path / 'state' / 'albedo' / 'beta'
    assert runtime.worktree_root == tmp_path / 'data' / 'albedo' / 'worktrees' / 'beta'


def test_resolve_runtime_config_honours_global_overrides(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        f'state_dir: {tmp_path / "s"}\nworktree_root: {tmp_path / "w"}\n' + _MIN_GLOBAL,
    )
    cfg = load_config(cfg_path)
    runtime = resolve_runtime_config(
        cfg, _make_manifest('alpha', 'Alpha'), Path('/tmp/alpha'), 'p-id'
    )
    assert runtime.state_dir == (tmp_path / 's' / 'alpha').absolute()
    assert runtime.worktree_root == (tmp_path / 'w' / 'alpha').absolute()


def test_models_default_is_all_none(tmp_path: Path) -> None:
    cfg_path = _write(tmp_path, _MIN_GLOBAL)
    cfg = load_config(cfg_path)
    assert cfg.models.coder is None
    assert cfg.models.reviewer is None
    assert cfg.models.architect is None
    assert cfg.models.triage is None


def test_models_load_and_dispatch_by_role(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        'models:\n'
        '  coder: claude-sonnet-4-6\n'
        '  reviewer: claude-haiku-4-5\n'
        '  architect: claude-opus-4-7\n'
        '  triage: claude-haiku-4-5\n' + _MIN_GLOBAL,
    )
    cfg = load_config(cfg_path)
    assert cfg.models.coder == 'claude-sonnet-4-6'
    assert cfg.models.for_role('CODER') == 'claude-sonnet-4-6'
    assert cfg.models.for_role('REVIEWER') == 'claude-haiku-4-5'
    assert cfg.models.for_role('ARCHITECT') == 'claude-opus-4-7'
    assert cfg.models.for_role('UNKNOWN') is None


def test_models_propagate_to_runtime_config(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        'models:\n  coder: claude-sonnet-4-6\n  triage: claude-haiku-4-5\n'
        + _MIN_GLOBAL,
    )
    cfg = load_config(cfg_path)
    runtime = resolve_runtime_config(cfg, _make_manifest(), Path('/tmp/r'), 'p-id')
    assert runtime.models.coder == 'claude-sonnet-4-6'
    assert runtime.models.triage == 'claude-haiku-4-5'


def test_max_attempts_before_escalation_default_and_propagation(
    tmp_path: Path,
) -> None:
    cfg_path = _write(tmp_path, _MIN_GLOBAL)
    cfg = load_config(cfg_path)
    assert cfg.max_attempts_before_escalation == 3
    runtime = resolve_runtime_config(cfg, _make_manifest(), Path('/tmp/r'), 'p-id')
    assert runtime.max_attempts_before_escalation == 3


def test_max_attempts_before_escalation_override(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        'max_attempts_before_escalation: 5\n' + _MIN_GLOBAL,
    )
    cfg = load_config(cfg_path)
    assert cfg.max_attempts_before_escalation == 5


def test_max_attempts_before_escalation_rejects_zero(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        'max_attempts_before_escalation: 0\n' + _MIN_GLOBAL,
    )
    with pytest.raises(ValidationError):
        load_config(cfg_path)


def test_load_linear_api_key_returns_secret() -> None:
    secret = load_linear_api_key({LINEAR_API_KEY_ENV: 'lin_api_xyz'})
    assert secret.get_secret_value() == 'lin_api_xyz'


def test_load_linear_api_key_strips_whitespace() -> None:
    secret = load_linear_api_key({LINEAR_API_KEY_ENV: '  lin_api_xyz  '})
    assert secret.get_secret_value() == 'lin_api_xyz'


def test_load_linear_api_key_raises_when_explicit_env_missing() -> None:
    with pytest.raises(RuntimeError, match=LINEAR_API_KEY_ENV):
        load_linear_api_key({})


def test_load_linear_api_key_raises_when_explicit_env_blank() -> None:
    with pytest.raises(RuntimeError, match=LINEAR_API_KEY_ENV):
        load_linear_api_key({LINEAR_API_KEY_ENV: '   '})


def test_load_linear_api_key_uses_real_environ_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_secrets_env(monkeypatch, tmp_path)
    monkeypatch.setenv(LINEAR_API_KEY_ENV, 'lin_real')
    assert load_linear_api_key().get_secret_value() == 'lin_real'


def test_load_linear_api_key_reads_from_dotenv_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LINEAR_API_KEY_ENV, raising=False)
    env_path = tmp_path / 'envfile'
    env_path.write_text(f'{LINEAR_API_KEY_ENV}=lin_from_file\n', encoding='utf-8')
    secret = load_linear_api_key(env_file=env_path)
    assert secret.get_secret_value() == 'lin_from_file'


def test_load_linear_api_key_process_env_beats_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / 'envfile'
    env_path.write_text(f'{LINEAR_API_KEY_ENV}=from_file\n', encoding='utf-8')
    monkeypatch.setenv(LINEAR_API_KEY_ENV, 'from_process')
    secret = load_linear_api_key(env_file=env_path)
    assert secret.get_secret_value() == 'from_process'


def test_load_linear_api_key_raises_when_no_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_secrets_env(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match=LINEAR_API_KEY_ENV):
        load_linear_api_key()


def test_load_linear_api_key_raises_when_dotenv_blank(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LINEAR_API_KEY_ENV, raising=False)
    env_path = tmp_path / 'envfile'
    env_path.write_text(f'{LINEAR_API_KEY_ENV}=\n', encoding='utf-8')
    with pytest.raises(RuntimeError, match=LINEAR_API_KEY_ENV):
        load_linear_api_key(env_file=env_path)


def test_load_linear_api_key_picks_agent_specific_from_explicit_env() -> None:
    secret = load_linear_api_key(
        {LINEAR_API_KEY_ENV: 'shared', f'{LINEAR_API_KEY_ENV}_1': 'agent-one'},
        agent_id='1',
    )
    assert secret.get_secret_value() == 'agent-one'


def test_load_linear_api_key_falls_back_to_shared_for_agent() -> None:
    secret = load_linear_api_key({LINEAR_API_KEY_ENV: 'shared'}, agent_id='2')
    assert secret.get_secret_value() == 'shared'


def test_load_linear_api_key_sanitizes_agent_id() -> None:
    secret = load_linear_api_key(
        {f'{LINEAR_API_KEY_ENV}_AG_NT_X': 'special'},
        agent_id='ag-nt.x',
    )
    assert secret.get_secret_value() == 'special'


def test_load_linear_api_key_agent_specific_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LINEAR_API_KEY_ENV, raising=False)
    monkeypatch.delenv(f'{LINEAR_API_KEY_ENV}_2', raising=False)
    env_path = tmp_path / 'envfile'
    env_path.write_text(
        f'{LINEAR_API_KEY_ENV}=shared\n{LINEAR_API_KEY_ENV}_2=agent-two\n',
        encoding='utf-8',
    )
    secret = load_linear_api_key(env_file=env_path, agent_id='2')
    assert secret.get_secret_value() == 'agent-two'


def test_load_linear_api_key_agent_specific_dotenv_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LINEAR_API_KEY_ENV, raising=False)
    monkeypatch.delenv(f'{LINEAR_API_KEY_ENV}_3', raising=False)
    env_path = tmp_path / 'envfile'
    env_path.write_text(f'{LINEAR_API_KEY_ENV}=shared\n', encoding='utf-8')
    secret = load_linear_api_key(env_file=env_path, agent_id='3')
    assert secret.get_secret_value() == 'shared'


def test_load_linear_api_key_agent_specific_missing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_secrets_env(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match='LINEAR_API_KEY_4'):
        load_linear_api_key(agent_id='4')


def test_load_linear_api_key_explicit_env_agent_missing_raises() -> None:
    with pytest.raises(RuntimeError, match='LINEAR_API_KEY_5'):
        load_linear_api_key({}, agent_id='5')


def test_load_github_pat_returns_none_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_secrets_env(monkeypatch, tmp_path)
    assert load_github_pat() is None


def test_load_github_pat_required_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_secrets_env(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match=GITHUB_PAT_ENV):
        load_github_pat(required=True)


def test_load_github_pat_reads_dotenv(tmp_path: Path) -> None:
    env_path = tmp_path / 'envfile'
    env_path.write_text(f'{GITHUB_PAT_ENV}=ghp_xyz\n', encoding='utf-8')
    secret = load_github_pat(env_file=env_path)
    assert secret is not None
    assert secret.get_secret_value() == 'ghp_xyz'


def test_load_github_pat_picks_agent_specific_from_explicit_env() -> None:
    secret = load_github_pat(
        {GITHUB_PAT_ENV: 'shared', f'{GITHUB_PAT_ENV}_1': 'agent-one'},
        agent_id='1',
    )
    assert secret is not None
    assert secret.get_secret_value() == 'agent-one'


def test_load_github_pat_falls_back_to_shared_for_agent() -> None:
    secret = load_github_pat({GITHUB_PAT_ENV: 'shared'}, agent_id='2')
    assert secret is not None
    assert secret.get_secret_value() == 'shared'


def test_load_github_pat_sanitizes_agent_id() -> None:
    secret = load_github_pat(
        {f'{GITHUB_PAT_ENV}_AG_NT_X': 'special'},
        agent_id='ag-nt.x',
    )
    assert secret is not None
    assert secret.get_secret_value() == 'special'


def test_load_github_pat_agent_specific_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(GITHUB_PAT_ENV, raising=False)
    monkeypatch.delenv(f'{GITHUB_PAT_ENV}_2', raising=False)
    env_path = tmp_path / 'envfile'
    env_path.write_text(
        f'{GITHUB_PAT_ENV}=shared\n{GITHUB_PAT_ENV}_2=agent-two\n',
        encoding='utf-8',
    )
    secret = load_github_pat(env_file=env_path, agent_id='2')
    assert secret is not None
    assert secret.get_secret_value() == 'agent-two'


def test_load_github_pat_agent_specific_dotenv_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(GITHUB_PAT_ENV, raising=False)
    monkeypatch.delenv(f'{GITHUB_PAT_ENV}_3', raising=False)
    env_path = tmp_path / 'envfile'
    env_path.write_text(f'{GITHUB_PAT_ENV}=shared\n', encoding='utf-8')
    secret = load_github_pat(env_file=env_path, agent_id='3')
    assert secret is not None
    assert secret.get_secret_value() == 'shared'


def test_load_github_pat_agent_specific_required_missing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_secrets_env(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match=f'{GITHUB_PAT_ENV}_4'):
        load_github_pat(agent_id='4', required=True)


def test_load_github_pat_agent_specific_missing_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_secrets_env(monkeypatch, tmp_path)
    assert load_github_pat(agent_id='4') is None


def test_load_bot_identity_returns_none_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_secrets_env(monkeypatch, tmp_path)
    assert load_bot_identity() is None


def test_load_bot_identity_returns_none_when_only_name_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_secrets_env(monkeypatch, tmp_path)
    monkeypatch.setenv(GITHUB_BOT_NAME_ENV, 'Bot')
    assert load_bot_identity() is None


def test_load_bot_identity_explicit_env_returns_pair() -> None:
    identity = load_bot_identity(
        {GITHUB_BOT_NAME_ENV: 'Albedo Bot', GITHUB_BOT_EMAIL_ENV: 'bot@example.com'}
    )
    assert identity == ('Albedo Bot', 'bot@example.com')


def test_load_bot_identity_picks_agent_specific_from_explicit_env() -> None:
    identity = load_bot_identity(
        {
            GITHUB_BOT_NAME_ENV: 'Shared',
            GITHUB_BOT_EMAIL_ENV: 'shared@example.com',
            f'{GITHUB_BOT_NAME_ENV}_1': 'Agent One',
            f'{GITHUB_BOT_EMAIL_ENV}_1': 'one@example.com',
        },
        agent_id='1',
    )
    assert identity == ('Agent One', 'one@example.com')


def test_load_bot_identity_falls_back_to_shared_for_agent() -> None:
    identity = load_bot_identity(
        {GITHUB_BOT_NAME_ENV: 'Shared', GITHUB_BOT_EMAIL_ENV: 'shared@example.com'},
        agent_id='2',
    )
    assert identity == ('Shared', 'shared@example.com')


def test_load_bot_identity_reads_from_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(GITHUB_BOT_NAME_ENV, raising=False)
    monkeypatch.delenv(GITHUB_BOT_EMAIL_ENV, raising=False)
    env_path = tmp_path / 'envfile'
    env_path.write_text(
        f'{GITHUB_BOT_NAME_ENV}=Albedo Bot\n{GITHUB_BOT_EMAIL_ENV}=bot@example.com\n',
        encoding='utf-8',
    )
    identity = load_bot_identity(env_file=env_path)
    assert identity == ('Albedo Bot', 'bot@example.com')
