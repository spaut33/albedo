"""Tests for the Linear bootstrap script."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from albedo import setup as orchestrator_setup
from albedo.linear_client import LinearClient
from albedo.setup import (
    REQUIRED_LABELS,
    REQUIRED_STATES,
    BootstrapReport,
    bootstrap,
    main,
)


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> LinearClient:
    return LinearClient(
        'https://api.linear.app/graphql',
        SecretStr('lin_api_test'),
        transport=httpx.MockTransport(handler),
        max_retries=1,
        backoff_seconds=0.0,
    )


def _data(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={'data': payload})


def test_bootstrap_creates_everything_when_team_is_empty() -> None:
    state_calls: list[str] = []
    label_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode('utf-8')
        if 'TeamByKey' in body:
            return _data(
                {
                    'teams': {
                        'nodes': [{'id': 't1', 'key': 'ORC', 'name': 'Orchestrator'}]
                    }
                }
            )
        if 'query States' in body:
            return _data({'workflowStates': {'nodes': []}})
        if 'query Labels' in body:
            return _data({'issueLabels': {'nodes': []}})
        if 'CreateState' in body:
            state_calls.append(body)
            return _data(
                {
                    'workflowStateCreate': {
                        'success': True,
                        'workflowState': {
                            'id': f's{len(state_calls)}',
                            'name': f'state-{len(state_calls)}',
                            'type': 'backlog',
                        },
                    }
                }
            )
        if 'CreateLabel' in body:
            label_calls.append(body)
            return _data(
                {
                    'issueLabelCreate': {
                        'success': True,
                        'issueLabel': {
                            'id': f'l{len(label_calls)}',
                            'name': f'label-{len(label_calls)}',
                        },
                    }
                }
            )
        raise AssertionError(f'Unexpected request: {body[:120]}')

    with _make_client(handler) as client:
        report = bootstrap(client, 'ORC')

    assert isinstance(report, BootstrapReport)
    assert len(report.states_created) == len(REQUIRED_STATES)
    assert len(report.states_planned) == len(REQUIRED_STATES)
    assert len(report.labels_created) == len(REQUIRED_LABELS)
    assert len(report.labels_planned) == len(REQUIRED_LABELS)
    assert report.states_existing == []
    assert report.labels_existing == []
    assert report.dry_run is False


def test_bootstrap_is_noop_when_everything_exists() -> None:
    states_payload = [
        {'id': f's{i}', 'name': spec.name, 'type': spec.type}
        for i, spec in enumerate(REQUIRED_STATES)
    ]
    labels_payload = [
        {'id': f'l{i}', 'name': spec.name} for i, spec in enumerate(REQUIRED_LABELS)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode('utf-8')
        if 'TeamByKey' in body:
            return _data(
                {
                    'teams': {
                        'nodes': [{'id': 't1', 'key': 'ORC', 'name': 'Orchestrator'}]
                    }
                }
            )
        if 'query States' in body:
            return _data({'workflowStates': {'nodes': states_payload}})
        if 'query Labels' in body:
            return _data({'issueLabels': {'nodes': labels_payload}})
        raise AssertionError(f'Unexpected mutation when team is fully set up: {body}')

    with _make_client(handler) as client:
        report = bootstrap(client, 'ORC')

    assert report.states_created == []
    assert report.states_planned == []
    assert report.labels_created == []
    assert report.labels_planned == []
    assert len(report.states_existing) == len(REQUIRED_STATES)
    assert len(report.labels_existing) == len(REQUIRED_LABELS)


def test_bootstrap_creates_only_missing() -> None:
    existing_state = REQUIRED_STATES[0]
    existing_label = REQUIRED_LABELS[0]
    create_state_calls: list[str] = []
    create_label_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode('utf-8')
        if 'TeamByKey' in body:
            return _data(
                {
                    'teams': {
                        'nodes': [{'id': 't1', 'key': 'ORC', 'name': 'Orchestrator'}]
                    }
                }
            )
        if 'query States' in body:
            return _data(
                {
                    'workflowStates': {
                        'nodes': [
                            {
                                'id': 's-existing',
                                'name': existing_state.name,
                                'type': existing_state.type,
                            }
                        ]
                    }
                }
            )
        if 'query Labels' in body:
            return _data(
                {
                    'issueLabels': {
                        'nodes': [{'id': 'l-existing', 'name': existing_label.name}]
                    }
                }
            )
        if 'CreateState' in body:
            create_state_calls.append(body)
            return _data(
                {
                    'workflowStateCreate': {
                        'success': True,
                        'workflowState': {
                            'id': f's{len(create_state_calls)}',
                            'name': 'x',
                            'type': 'backlog',
                        },
                    }
                }
            )
        if 'CreateLabel' in body:
            create_label_calls.append(body)
            return _data(
                {
                    'issueLabelCreate': {
                        'success': True,
                        'issueLabel': {
                            'id': f'l{len(create_label_calls)}',
                            'name': 'x',
                        },
                    }
                }
            )
        raise AssertionError(f'Unexpected request: {body[:120]}')

    with _make_client(handler) as client:
        report = bootstrap(client, 'ORC')

    assert len(report.states_existing) == 1
    assert len(report.states_created) == len(REQUIRED_STATES) - 1
    assert len(report.states_planned) == len(REQUIRED_STATES) - 1
    assert len(report.labels_existing) == 1
    assert len(report.labels_created) == len(REQUIRED_LABELS) - 1
    assert len(report.labels_planned) == len(REQUIRED_LABELS) - 1


def test_bootstrap_report_summary_lists_creates() -> None:
    from albedo.linear_client import IssueLabel, Team, WorkflowState
    from albedo.setup import LabelSpec, StateSpec

    report = BootstrapReport(
        team=Team(id='t', key='ORC', name='Orchestrator'),
        states_existing=[WorkflowState(id='s1', name='Backlog', type='backlog')],
        states_created=[WorkflowState(id='s2', name='Review', type='started')],
        states_planned=[StateSpec('Review', 'started', '#fff')],
        labels_existing=[IssueLabel(id='l1', name='draft')],
        labels_created=[IssueLabel(id='l2', name='stuck')],
        labels_planned=[LabelSpec('stuck', '#fff')],
        projects=[],
        dry_run=False,
    )
    text = report.summary()
    assert 'Orchestrator (ORC)' in text
    assert '+ state Review' in text
    assert '+ label stuck' in text


def test_bootstrap_report_summary_marks_dry_run() -> None:
    from albedo.linear_client import Team
    from albedo.setup import LabelSpec, StateSpec

    report = BootstrapReport(
        team=Team(id='t', key='ORC', name='Orchestrator'),
        states_existing=[],
        states_created=[],
        states_planned=[StateSpec('Review', 'started', '#fff')],
        labels_existing=[],
        labels_created=[],
        labels_planned=[LabelSpec('stuck', '#fff')],
        projects=[],
        dry_run=True,
    )
    text = report.summary()
    assert 'DRY RUN' in text
    assert '+ state Review' in text
    assert '+ label stuck' in text


def test_bootstrap_report_summary_lists_project_failures() -> None:
    from albedo.linear_client import Project, Team
    from albedo.setup import ProjectVerification

    report = BootstrapReport(
        team=Team(id='t', key='ORC', name='Orchestrator'),
        states_existing=[],
        states_created=[],
        states_planned=[],
        labels_existing=[],
        labels_created=[],
        labels_planned=[],
        projects=[
            ProjectVerification(
                profile_name='sample',
                linear_name='Sample',
                found=Project(id='p-1', name='Sample'),
                error=None,
            ),
            ProjectVerification(
                profile_name='broken',
                linear_name='Missing',
                found=None,
                error='not found for team',
            ),
        ],
        dry_run=False,
    )
    text = report.summary()
    assert '✓ sample' in text
    assert '✗ broken' in text
    assert report.has_project_failures is True


def test_bootstrap_dry_run_does_not_call_create() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode('utf-8')
        if 'TeamByKey' in body:
            return _data(
                {
                    'teams': {
                        'nodes': [{'id': 't1', 'key': 'ORC', 'name': 'Orchestrator'}]
                    }
                }
            )
        if 'query States' in body:
            return _data({'workflowStates': {'nodes': []}})
        if 'query Labels' in body:
            return _data({'issueLabels': {'nodes': []}})
        raise AssertionError(f'Mutation called in dry-run: {body[:120]}')

    with _make_client(handler) as client:
        report = bootstrap(client, 'ORC', dry_run=True)

    assert report.dry_run is True
    assert report.states_created == []
    assert report.labels_created == []
    assert len(report.states_planned) == len(REQUIRED_STATES)
    assert len(report.labels_planned) == len(REQUIRED_LABELS)


def test_bootstrap_resolves_team_by_name_when_key_lookup_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode('utf-8')
        if 'TeamByKey' in body:
            return _data({'teams': {'nodes': []}})
        if 'TeamByName' in body:
            return _data(
                {'teams': {'nodes': [{'id': 't1', 'key': 'AIT', 'name': 'AI-Team'}]}}
            )
        if 'query States' in body:
            return _data({'workflowStates': {'nodes': []}})
        if 'query Labels' in body:
            return _data({'issueLabels': {'nodes': []}})
        raise AssertionError(f'Unexpected: {body[:120]}')

    with _make_client(handler) as client:
        report = bootstrap(client, 'AI-Team', dry_run=True)

    assert report.team.name == 'AI-Team'
    assert report.team.key == 'AIT'


def test_main_runs_bootstrap_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg_path = tmp_path / 'config.yaml'
    cfg_path.write_text(
        'workers: 1\nlinear:\n  team: ORC\n',
        encoding='utf-8',
    )
    monkeypatch.setenv('LINEAR_API_KEY', 'lin_api_test')

    # Stub manifest discovery so setup picks up the per-repo project.
    from albedo.repo_config import (
        RepoManifest,
        RepoManifestLinear,
        RepoManifestRepo,
    )

    def _load_repo_manifest(_start: Path) -> tuple[Path, RepoManifest]:
        return (
            tmp_path,
            RepoManifest(
                name='sample',
                linear=RepoManifestLinear(project='Sample'),
                repo=RepoManifestRepo(),
            ),
        )

    monkeypatch.setattr(orchestrator_setup, 'load_repo_manifest', _load_repo_manifest)

    captured: dict[str, Any] = {}

    class StubClient:
        def __init__(self, api_url: str, api_key: SecretStr) -> None:
            captured['api_url'] = api_url
            captured['api_key'] = api_key.get_secret_value()

        def __enter__(self) -> StubClient:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def fake_bootstrap(
        _client: object,
        team_identifier: str,
        *,
        project_profiles: dict[str, str] | None = None,
        dry_run: bool = False,
    ) -> BootstrapReport:
        from albedo.linear_client import Team

        captured['team_identifier'] = team_identifier
        captured['project_profiles'] = project_profiles
        captured['dry_run'] = dry_run
        return BootstrapReport(
            team=Team(id='t', key=team_identifier, name='Orchestrator'),
            states_existing=[],
            states_created=[],
            states_planned=[],
            labels_existing=[],
            labels_created=[],
            labels_planned=[],
            projects=[],
            dry_run=dry_run,
        )

    monkeypatch.setattr(orchestrator_setup, 'LinearClient', StubClient)
    monkeypatch.setattr(orchestrator_setup, 'bootstrap', fake_bootstrap)

    exit_code = main(['--config', str(cfg_path), '--dry-run'])

    assert exit_code == 0
    assert captured['team_identifier'] == 'ORC'
    assert captured['dry_run'] is True
    assert captured['project_profiles'] == {'sample': 'Sample'}
    assert captured['api_url'] == 'https://api.linear.app/graphql'
    assert captured['api_key'] == 'lin_api_test'
    out = capsys.readouterr().out
    assert 'Orchestrator (ORC)' in out
    assert 'DRY RUN' in out
