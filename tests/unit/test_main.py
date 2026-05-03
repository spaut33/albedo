"""Smoke tests for the CLI entrypoint."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from albedo import __main__ as cli
from albedo import __version__
from albedo.__main__ import main
from albedo.claude_runner import ClaudeRunResult
from albedo.dispatch import CODER
from albedo.linear_client import Issue, Viewer
from albedo.repo_config import RepoManifest, RepoManifestLinear, RepoManifestRepo
from albedo.supervisor import SupervisorOptions
from albedo.worker import RunOnceResult
from albedo.worktree import WorktreeInfo


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(['--version'])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out


def test_unknown_argument_exits_with_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main(['--no-such-flag'])
    assert exc.value.code != 0


def test_once_without_issue_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(['--once'])
    err = capsys.readouterr().err
    assert exit_code == 2
    assert '--issue' in err


_GLOBAL_YAML = 'workers: {workers}\nlinear:\n  team: ORC\n'


def _global_yaml(*, workers: int = 1) -> str:
    return _GLOBAL_YAML.format(workers=workers)


def _fake_manifest() -> RepoManifest:
    return RepoManifest(
        name='sample',
        linear=RepoManifestLinear(project='Sample'),
        repo=RepoManifestRepo(),
    )


def _install_fakes(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    """Stub Linear lookup + manifest discovery so main() doesn't hit the network."""
    monkeypatch.setattr(
        cli,
        'load_repo_manifest',
        lambda _start: (repo_root, _fake_manifest()),
    )
    monkeypatch.setattr(
        cli,
        '_resolve_project_id',
        lambda _file_config, _manifest: 'prj-uuid',
    )


@dataclass
class _StubClient:
    api_url: str
    api_key: object

    def __enter__(self) -> _StubClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def viewer(self) -> Viewer:
        return Viewer(id='u-1', name='Roman', email='r@x.com')


def _stub_run_once_result(*, is_error: bool = False) -> RunOnceResult:
    issue = Issue(
        id='uuid-1',
        identifier='AI-5',
        title='Add filter',
        description='',
        state_id='s',
        state_name='Backlog',
        assignee_id=None,
        label_ids=(),
        label_names=(),
        parent_id=None,
        branch_name='',
    )
    return RunOnceResult(
        issue=issue,
        role=CODER,
        worktree=WorktreeInfo(
            path=Path('/tmp/wt/sample-ai-5'),
            branch='task/ai-5',
            base_branch='main',
        ),
        claude=ClaudeRunResult(
            is_error=is_error,
            exit_code=0 if not is_error else 1,
            result_text='done',
            total_cost_usd=0.0,
            usage={'input_tokens': 5},
        ),
    )


def test_once_invokes_run_once_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_global_yaml(workers=1), encoding='utf-8')
    monkeypatch.setenv('LINEAR_API_KEY', 'lin_test')
    _install_fakes(monkeypatch, tmp_path)

    captured: dict[str, object] = {}

    def fake_run_once(**kwargs: object) -> RunOnceResult:
        captured.update(kwargs)
        return _stub_run_once_result()

    monkeypatch.setattr(cli, 'LinearClient', _StubClient)
    monkeypatch.setattr(cli, 'run_once', fake_run_once)

    exit_code = main(
        ['--once', '--issue', 'AI-5', '--config', str(cfg), '--mcp-config', '']
    )

    assert exit_code == 0
    assert captured['issue_identifier'] == 'AI-5'
    assert captured['agent_id'] == '1'
    out = capsys.readouterr().out
    assert 'AI-5' in out and 'CODER' in out
    assert 'input_tokens=5' in out


def test_once_returns_1_on_claude_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_global_yaml(workers=1), encoding='utf-8')
    monkeypatch.setenv('LINEAR_API_KEY', 'lin_test')
    _install_fakes(monkeypatch, tmp_path)

    monkeypatch.setattr(cli, 'LinearClient', _StubClient)

    def fake_run_once_err(**_: object) -> RunOnceResult:
        return _stub_run_once_result(is_error=True)

    monkeypatch.setattr(cli, 'run_once', fake_run_once_err)

    exit_code = main(
        ['--once', '--issue', 'AI-5', '--config', str(cfg), '--mcp-config', '']
    )
    assert exit_code == 1


def test_default_mode_calls_supervise_with_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_global_yaml(workers=3), encoding='utf-8')
    monkeypatch.setenv('LINEAR_API_KEY', 'lin_test')
    _install_fakes(monkeypatch, tmp_path)

    captured: list[SupervisorOptions] = []

    def fake_supervise(options: SupervisorOptions) -> None:
        captured.append(options)

    monkeypatch.setattr(cli, 'supervise', fake_supervise)

    exit_code = main(['--config', str(cfg), '--mcp-config', ''])
    assert exit_code == 0
    assert len(captured) == 1
    assert captured[0].workers == 3
    assert captured[0].skip_stale_recovery is False


def test_workers_flag_overrides_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_global_yaml(workers=1), encoding='utf-8')
    monkeypatch.setenv('LINEAR_API_KEY', 'lin_test')
    _install_fakes(monkeypatch, tmp_path)

    captured: list[SupervisorOptions] = []

    def _capture(options: SupervisorOptions) -> None:
        captured.append(options)

    monkeypatch.setattr(cli, 'supervise', _capture)

    exit_code = main(['--workers', '5', '--config', str(cfg), '--mcp-config', ''])
    assert exit_code == 0
    assert captured[0].workers == 5


def test_workers_flag_rejects_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_global_yaml(workers=1), encoding='utf-8')
    monkeypatch.setenv('LINEAR_API_KEY', 'lin_test')
    _install_fakes(monkeypatch, tmp_path)
    exit_code = main(['--workers', '0', '--config', str(cfg), '--mcp-config', ''])
    assert exit_code == 2


def test_skip_stale_recovery_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(_global_yaml(workers=1), encoding='utf-8')
    monkeypatch.setenv('LINEAR_API_KEY', 'lin_test')
    _install_fakes(monkeypatch, tmp_path)

    captured: list[SupervisorOptions] = []

    def _capture(options: SupervisorOptions) -> None:
        captured.append(options)

    monkeypatch.setattr(cli, 'supervise', _capture)

    exit_code = main(
        ['--skip-stale-recovery', '--config', str(cfg), '--mcp-config', '']
    )
    assert exit_code == 0
    assert captured[0].skip_stale_recovery is True
