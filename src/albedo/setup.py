"""Idempotent Linear bootstrap.

Run as `albedo setup` once per Linear team. Ensures every
workflow state and label the orchestrator depends on exists. Re-running is a
no-op when everything is already in place.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from albedo.config import (
    OrchestratorFileConfig,
    load_config,
    load_linear_api_key,
)
from albedo.linear_client import (
    IssueLabel,
    LinearClient,
    LinearError,
    Project,
    Team,
    WorkflowState,
)
from albedo.repo_config import RepoManifestNotFoundError, load_repo_manifest


@dataclass(frozen=True, slots=True)
class StateSpec:
    name: str
    type: str
    color: str


@dataclass(frozen=True, slots=True)
class LabelSpec:
    name: str
    color: str


REQUIRED_STATES: tuple[StateSpec, ...] = (
    StateSpec('Triage', 'unstarted', '#f7c8c8'),
    StateSpec('Backlog', 'backlog', '#bec2c8'),
    StateSpec('Todo', 'unstarted', '#e2e8f0'),
    StateSpec('In Progress', 'started', '#7c3aed'),
    StateSpec('Review', 'started', '#5e6ad2'),
    StateSpec('Awaiting approval', 'started', '#f2c94c'),
    StateSpec('Done', 'completed', '#27ae60'),
    StateSpec('Canceled', 'canceled', '#95a2b3'),
)

REQUIRED_LABELS: tuple[LabelSpec, ...] = (
    LabelSpec('stuck', '#ef4444'),
    LabelSpec('needs-coordination', '#f59e0b'),
    LabelSpec('blocked-external', '#94a3b8'),
    LabelSpec('kind:decomposition', '#6366f1'),
    LabelSpec('kind:final-pr', '#10b981'),
    LabelSpec('attempts:1', '#fde68a'),
    LabelSpec('attempts:2', '#fb923c'),
    LabelSpec('attempts:3', '#dc2626'),
    LabelSpec('awaiting-human-reply', '#fbbf24'),
    LabelSpec('cancelled-by-human', '#9ca3af'),
)


@dataclass(frozen=True, slots=True)
class ProjectVerification:
    profile_name: str
    linear_name: str
    found: Project | None
    error: str | None


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    team: Team
    states_existing: list[WorkflowState]
    states_created: list[WorkflowState]
    states_planned: list[StateSpec]
    labels_existing: list[IssueLabel]
    labels_created: list[IssueLabel]
    labels_planned: list[LabelSpec]
    projects: list[ProjectVerification]
    dry_run: bool

    @property
    def has_project_failures(self) -> bool:
        return any(p.error is not None for p in self.projects)

    def summary(self) -> str:
        verb = 'planned' if self.dry_run else 'created'
        lines = [
            f'Team: {self.team.name} ({self.team.key}) [{self.team.id}]',
        ]
        if self.dry_run:
            lines.append('Mode: DRY RUN — no changes were written to Linear.')
        lines.append(
            f'States: {len(self.states_existing)} existing, '
            f'{len(self.states_planned)} {verb}'
        )
        for spec in self.states_planned:
            lines.append(f'  + state {spec.name} ({spec.type})')
        lines.append(
            f'Labels: {len(self.labels_existing)} existing, '
            f'{len(self.labels_planned)} {verb}'
        )
        for spec in self.labels_planned:
            lines.append(f'  + label {spec.name}')
        lines.append(f'Projects: {len(self.projects)} verified')
        for p in self.projects:
            if p.error is None:
                pid = p.found.id if p.found else '?'
                lines.append(f'  ✓ {p.profile_name} → {p.linear_name!r} [{pid}]')
            else:
                lines.append(f'  ✗ {p.profile_name} → {p.linear_name!r}: {p.error}')
        return '\n'.join(lines)


def bootstrap(
    client: LinearClient,
    team_identifier: str,
    *,
    project_profiles: dict[str, str] | None = None,
    dry_run: bool = False,
) -> BootstrapReport:
    """Reconcile Linear state to match REQUIRED_STATES and REQUIRED_LABELS.

    `team_identifier` may be a Linear team key (e.g. 'ORC') or an exact team
    name (e.g. 'AI-Team'). When `dry_run` is true, no mutations are performed.

    `project_profiles` maps profile name → Linear project name. Each
    project is looked up read-only and reported in `BootstrapReport.projects`;
    bootstrap never creates projects (operators provision those by hand
    in the Linear UI).
    """
    team = client.resolve_team(team_identifier)
    states_existing, states_created, states_planned = _ensure_states(
        client, team.id, dry_run=dry_run
    )
    labels_existing, labels_created, labels_planned = _ensure_labels(
        client, team.id, dry_run=dry_run
    )
    projects = _verify_projects(client, team.id, project_profiles or {})
    return BootstrapReport(
        team=team,
        states_existing=states_existing,
        states_created=states_created,
        states_planned=states_planned,
        labels_existing=labels_existing,
        labels_created=labels_created,
        labels_planned=labels_planned,
        projects=projects,
        dry_run=dry_run,
    )


def _verify_projects(
    client: LinearClient,
    team_id: str,
    profiles: dict[str, str],
) -> list[ProjectVerification]:
    results: list[ProjectVerification] = []
    for profile_name, linear_name in profiles.items():
        try:
            project = client.get_project_by_name(team_id, linear_name)
            results.append(
                ProjectVerification(
                    profile_name=profile_name,
                    linear_name=linear_name,
                    found=project,
                    error=None,
                )
            )
        except LinearError as exc:
            results.append(
                ProjectVerification(
                    profile_name=profile_name,
                    linear_name=linear_name,
                    found=None,
                    error=str(exc),
                )
            )
    return results


def _ensure_states(
    client: LinearClient, team_id: str, *, dry_run: bool
) -> tuple[list[WorkflowState], list[WorkflowState], list[StateSpec]]:
    current = client.list_workflow_states(team_id)
    by_name = {s.name: s for s in current}
    existing: list[WorkflowState] = []
    created: list[WorkflowState] = []
    planned: list[StateSpec] = []
    for spec in REQUIRED_STATES:
        if spec.name in by_name:
            existing.append(by_name[spec.name])
            continue
        planned.append(spec)
        if dry_run:
            continue
        created.append(
            client.create_workflow_state(team_id, spec.name, spec.type, spec.color)
        )
    return existing, created, planned


def _ensure_labels(
    client: LinearClient, team_id: str, *, dry_run: bool
) -> tuple[list[IssueLabel], list[IssueLabel], list[LabelSpec]]:
    current = client.list_issue_labels(team_id)
    by_name = {label.name: label for label in current}
    existing: list[IssueLabel] = []
    created: list[IssueLabel] = []
    planned: list[LabelSpec] = []
    for spec in REQUIRED_LABELS:
        if spec.name in by_name:
            existing.append(by_name[spec.name])
            continue
        planned.append(spec)
        if dry_run:
            continue
        created.append(client.create_issue_label(team_id, spec.name, spec.color))
    return existing, created, planned


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='albedo setup',
        description='Idempotently bootstrap a Linear team for albedo.',
    )
    parser.add_argument(
        '--config',
        default=None,
        help='Path to global config.yaml (default: $ALBEDO_HOME/config.yaml).',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Report what would be created without mutating Linear.',
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config: OrchestratorFileConfig = load_config(args.config)
    api_key = load_linear_api_key()
    # If invoked inside a target repo, verify that repo's Linear project too;
    # otherwise just bootstrap the team-level states and labels.
    profiles: dict[str, str] | None = None
    try:
        _, manifest = load_repo_manifest(Path.cwd())
    except RepoManifestNotFoundError:
        profiles = None
    else:
        profiles = {manifest.name: manifest.linear.project}
    with LinearClient(config.linear.api_url, api_key) as client:
        report = bootstrap(
            client,
            config.linear.team,
            project_profiles=profiles,
            dry_run=args.dry_run,
        )
    print(report.summary())
    return 1 if report.has_project_failures else 0


if __name__ == '__main__':
    sys.exit(main())
