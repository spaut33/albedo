"""Minimal Linear GraphQL client used by the bootstrap setup and workers.

Scope is intentionally narrow: only the operations the orchestrator currently
needs (look up team by key, list/create workflow states, list/create labels).
New methods land here as later phases need them. We talk to Linear directly
via httpx rather than vendor SDKs so the surface stays small and explicit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, cast

import httpx
from pydantic import SecretStr

DEFAULT_TIMEOUT_SECONDS = 30.0
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _nested_id(node: dict[str, Any] | None) -> str | None:
    """Pull `.id` from a possibly-null nested GraphQL node."""
    if not node:
        return None
    value = node.get('id')
    return str(value) if value is not None else None


ISSUE_FRAGMENT = """
    id
    identifier
    title
    description
    url
    branchName
    state { id name }
    assignee { id }
    parent { id }
    labels { nodes { id name } }
    attachments { nodes { id url title subtitle } }
    inverseRelations(first: 25) {
      nodes {
        type
        issue { state { type } }
      }
    }
"""


def issue_from_node(node: dict[str, Any]) -> Issue:
    """Build an `Issue` from a GraphQL node that includes ISSUE_FRAGMENT."""
    labels_dict = cast('dict[str, Any]', node.get('labels') or {})
    labels_list = cast('list[dict[str, Any]]', labels_dict.get('nodes') or [])
    relations_dict = cast('dict[str, Any]', node.get('inverseRelations') or {})
    relation_nodes = cast('list[dict[str, Any]]', relations_dict.get('nodes') or [])
    incoming_list: list[IncomingRelation] = []
    for r in relation_nodes:
        issue_obj = cast('dict[str, Any]', r.get('issue') or {})
        state_obj = cast('dict[str, Any]', issue_obj.get('state') or {})
        incoming_list.append(
            IncomingRelation(
                type=str(r.get('type', '')),
                source_state_type=str(state_obj.get('type', '')),
            )
        )
    return Issue(
        id=node['id'],
        identifier=node['identifier'],
        title=node['title'],
        description=node.get('description') or '',
        url=node.get('url') or '',
        state_id=node['state']['id'],
        state_name=node['state']['name'],
        assignee_id=_nested_id(node.get('assignee')),
        label_ids=tuple(str(label['id']) for label in labels_list),
        label_names=tuple(str(label['name']) for label in labels_list),
        parent_id=_nested_id(node.get('parent')),
        branch_name=node.get('branchName') or '',
        incoming_relations=tuple(incoming_list),
        attachments=_attachments_from_node(node),
    )


def _attachments_from_node(node: dict[str, Any]) -> tuple[RawAttachment, ...]:
    container = cast('dict[str, Any]', node.get('attachments') or {})
    nodes = cast('list[dict[str, Any]]', container.get('nodes') or [])
    return tuple(
        RawAttachment(
            id=str(n['id']),
            url=str(n.get('url') or ''),
            title=str(n.get('title') or ''),
            subtitle=str(n.get('subtitle') or ''),
        )
        for n in nodes
        if n.get('url')
    )


class LinearError(RuntimeError):
    """Raised when Linear returns a GraphQL or transport error."""


@dataclass(frozen=True, slots=True)
class WorkflowState:
    id: str
    name: str
    type: str


@dataclass(frozen=True, slots=True)
class IssueLabel:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Team:
    id: str
    key: str
    name: str


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Viewer:
    id: str
    name: str
    email: str


@dataclass(frozen=True, slots=True)
class IncomingRelation:
    """A relation pointing TO this issue.

    `type` is Linear's relation type (e.g. 'blocks', 'duplicate', 'related').
    `source_state_type` is the state type of the OTHER side of the relation
    — used to decide whether a `blocks` relation is still active.
    """

    type: str
    source_state_type: str


@dataclass(frozen=True, slots=True)
class RawAttachment:
    """A Linear Attachment-API entry (separate from markdown-embedded assets).

    `url` may point to `uploads.linear.app` (uploaded file) or any external
    URL the user pasted as a link attachment. The fetcher decides what to
    download based on extension and origin.
    """

    id: str
    url: str
    title: str = ''
    subtitle: str = ''


@dataclass(frozen=True, slots=True)
class Issue:
    id: str
    identifier: str
    title: str
    description: str
    state_id: str
    state_name: str
    assignee_id: str | None
    label_ids: tuple[str, ...]
    label_names: tuple[str, ...]
    parent_id: str | None
    branch_name: str
    url: str = ''
    incoming_relations: tuple[IncomingRelation, ...] = ()
    attachments: tuple[RawAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class Comment:
    id: str
    body: str
    author_id: str | None
    created_at: str = ''
    attachments: tuple[RawAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class IssueUpdate:
    state_id: str | None = None
    assignee_id: str | None = None
    label_ids: tuple[str, ...] | None = None
    description: str | None = None
    unset_assignee: bool = False


class LinearClient:
    """Thin GraphQL wrapper around the Linear API.

    The token is held as `SecretStr` so it never appears in logs/repr.
    `transport` is exposed for tests; production callers should leave it None.
    """

    def __init__(
        self,
        api_url: str,
        api_key: SecretStr,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
    ) -> None:
        self._api_url = api_url
        self._api_key = api_key
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout_seconds,
            headers={
                'Content-Type': 'application/json',
                'Authorization': api_key.get_secret_value(),
            },
        )

    @property
    def api_key(self) -> SecretStr:
        """Expose the Linear token to sibling subsystems (e.g. attachment fetch).

        Held as `SecretStr` so accidental logging stays redacted; callers
        must explicitly call `.get_secret_value()` when they need the raw
        bytes for an outbound request.
        """
        return self._api_key

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LinearClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def query(
        self,
        document: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a GraphQL document and return the `data` payload.

        Retries on 429/5xx with linear backoff. Raises `LinearError` for any
        non-2xx final response or for any GraphQL `errors` array.
        """
        payload = {'query': document, 'variables': variables or {}}
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = self._client.post(self._api_url, json=payload)
            except httpx.HTTPError as exc:
                last_error = exc
                self._sleep(attempt)
                continue
            if response.status_code in RETRYABLE_STATUS:
                last_error = LinearError(
                    f'Linear returned retryable status {response.status_code}'
                )
                self._sleep(attempt)
                continue
            if response.status_code >= 400:
                raise LinearError(
                    f'Linear HTTP {response.status_code}: {response.text}'
                )
            body: dict[str, Any] = response.json()
            if body.get('errors'):
                raise LinearError(f'Linear GraphQL errors: {body["errors"]}')
            data = body.get('data')
            if not isinstance(data, dict):
                raise LinearError(f'Linear response missing data field: {body!r}')
            return data  # type: ignore[no-any-return]
        raise LinearError(
            f'Linear request failed after {self._max_retries} attempts: {last_error}'
        )

    def _sleep(self, attempt: int) -> None:
        time.sleep(self._backoff_seconds * (attempt + 1))

    def get_team_by_key(self, key: str) -> Team:
        document = """
            query TeamByKey($key: String!) {
              teams(filter: {key: {eq: $key}}, first: 1) {
                nodes { id key name }
              }
            }
        """
        data = self.query(document, {'key': key})
        nodes = data['teams']['nodes']
        if not nodes:
            raise LinearError(f'Linear team with key {key!r} not found')
        node = nodes[0]
        return Team(id=node['id'], key=node['key'], name=node['name'])

    def get_team_by_name(self, name: str) -> Team:
        document = """
            query TeamByName($name: String!) {
              teams(filter: {name: {eq: $name}}, first: 1) {
                nodes { id key name }
              }
            }
        """
        data = self.query(document, {'name': name})
        nodes = data['teams']['nodes']
        if not nodes:
            raise LinearError(f'Linear team with name {name!r} not found')
        node = nodes[0]
        return Team(id=node['id'], key=node['key'], name=node['name'])

    def resolve_team(self, identifier: str) -> Team:
        """Look up a team by key first, then by exact name.

        Use this when the orchestrator is configured with a human-friendly
        team identifier and we don't yet know whether it's a key or a name.
        """
        try:
            return self.get_team_by_key(identifier)
        except LinearError:
            return self.get_team_by_name(identifier)

    def get_project_by_name(self, team_id: str, name: str) -> Project:
        """Return the Linear project named `name` linked to `team_id`.

        Projects can span multiple teams, so filter by exact name and
        then check the project's `teams` connection contains our target.
        """
        document = """
            query ProjectByName($name: String!) {
              projects(filter: {name: {eq: $name}}, first: 25) {
                nodes {
                  id
                  name
                  teams { nodes { id } }
                }
              }
            }
        """
        data = self.query(document, {'name': name})
        nodes = data['projects']['nodes']
        for node in nodes:
            teams = node.get('teams', {}).get('nodes', [])
            if any(t.get('id') == team_id for t in teams):
                return Project(id=node['id'], name=node['name'])
        raise LinearError(f'Linear project {name!r} not found for team id={team_id!r}')

    def list_workflow_states(self, team_id: str) -> list[WorkflowState]:
        document = """
            query States($teamId: ID!) {
              workflowStates(filter: {team: {id: {eq: $teamId}}}, first: 100) {
                nodes { id name type }
              }
            }
        """
        data = self.query(document, {'teamId': team_id})
        return [
            WorkflowState(id=n['id'], name=n['name'], type=n['type'])
            for n in data['workflowStates']['nodes']
        ]

    def create_workflow_state(
        self,
        team_id: str,
        name: str,
        state_type: str,
        color: str = '#bec2c8',
    ) -> WorkflowState:
        document = """
            mutation CreateState($input: WorkflowStateCreateInput!) {
              workflowStateCreate(input: $input) {
                success
                workflowState { id name type }
              }
            }
        """
        variables = {
            'input': {
                'teamId': team_id,
                'name': name,
                'type': state_type,
                'color': color,
            }
        }
        data = self.query(document, variables)
        result = data['workflowStateCreate']
        if not result.get('success'):
            raise LinearError(f'Failed to create workflow state {name!r}')
        node = result['workflowState']
        return WorkflowState(id=node['id'], name=node['name'], type=node['type'])

    def list_issue_labels(self, team_id: str) -> list[IssueLabel]:
        document = """
            query Labels($teamId: ID!) {
              issueLabels(filter: {team: {id: {eq: $teamId}}}, first: 250) {
                nodes { id name }
              }
            }
        """
        data = self.query(document, {'teamId': team_id})
        return [
            IssueLabel(id=n['id'], name=n['name']) for n in data['issueLabels']['nodes']
        ]

    def create_issue_label(
        self,
        team_id: str,
        name: str,
        color: str = '#bec2c8',
    ) -> IssueLabel:
        document = """
            mutation CreateLabel($input: IssueLabelCreateInput!) {
              issueLabelCreate(input: $input) {
                success
                issueLabel { id name }
              }
            }
        """
        variables = {'input': {'teamId': team_id, 'name': name, 'color': color}}
        data = self.query(document, variables)
        result = data['issueLabelCreate']
        if not result.get('success'):
            raise LinearError(f'Failed to create issue label {name!r}')
        node = result['issueLabel']
        return IssueLabel(id=node['id'], name=node['name'])

    def get_issue(self, identifier: str) -> Issue:
        """Fetch an issue by either UUID or shorthand identifier (e.g. 'AI-5')."""
        document = (
            'query Issue($id: String!) { issue(id: $id) {' + ISSUE_FRAGMENT + '} }'
        )
        data = self.query(document, {'id': identifier})
        node = data.get('issue')
        if not node:
            raise LinearError(f'Linear issue {identifier!r} not found')
        return issue_from_node(node)

    def list_issues_by_identifiers(
        self, identifiers: list[str], *, limit: int = 100
    ) -> list[Issue]:
        """Fetch issues whose shorthand identifier is in `identifiers`.

        One GraphQL call regardless of len(identifiers). Missing identifiers
        are silently dropped — caller diff'ing the result against the input
        sees which were not found. `limit` caps the page size; with Linear's
        max of 250, callers wanting more should chunk.
        """
        if not identifiers:
            return []
        document = (
            'query IssuesByIds($filter: IssueFilter!, $first: Int!) { '
            'issues(filter: $filter, first: $first) { nodes {'
            + ISSUE_FRAGMENT
            + '} } }'
        )
        filt: dict[str, Any] = {'number': {'in': []}}
        # Linear's IssueFilter doesn't support shorthand identifiers
        # directly, but exposes team+number. Group by team prefix.
        prefix_to_numbers: dict[str, list[int]] = {}
        for ident in identifiers:
            try:
                prefix, num = ident.rsplit('-', 1)
                prefix_to_numbers.setdefault(prefix.upper(), []).append(int(num))
            except (ValueError, AttributeError):
                continue
        if not prefix_to_numbers:
            return []
        filt = {
            'or': [
                {'team': {'key': {'eq': prefix}}, 'number': {'in': numbers}}
                for prefix, numbers in prefix_to_numbers.items()
            ]
        }
        data = self.query(document, {'filter': filt, 'first': limit})
        return [issue_from_node(node) for node in data['issues']['nodes']]

    def update_issue(self, issue_id: str, update: IssueUpdate) -> Issue:
        """Apply a partial update to an issue.

        `update.unset_assignee=True` clears the assignee (used for un-claim);
        otherwise `assignee_id=None` is treated as "leave unchanged".
        """
        input_payload: dict[str, Any] = {}
        if update.state_id is not None:
            input_payload['stateId'] = update.state_id
        if update.unset_assignee:
            input_payload['assigneeId'] = None
        elif update.assignee_id is not None:
            input_payload['assigneeId'] = update.assignee_id
        if update.label_ids is not None:
            input_payload['labelIds'] = list(update.label_ids)
        if update.description is not None:
            input_payload['description'] = update.description
        if not input_payload:
            raise LinearError('IssueUpdate must change at least one field')

        document = (
            'mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) { '
            'issueUpdate(id: $id, input: $input) { success issue {'
            + ISSUE_FRAGMENT
            + '} } }'
        )
        data = self.query(document, {'id': issue_id, 'input': input_payload})
        result = data['issueUpdate']
        if not result.get('success'):
            raise LinearError(f'Failed to update issue {issue_id!r}')
        return issue_from_node(result['issue'])

    def viewer(self) -> Viewer:
        """Return the user the API token authenticates as."""
        document = """
            query Viewer {
              viewer { id name email }
            }
        """
        data = self.query(document)
        node = data['viewer']
        return Viewer(id=node['id'], name=node['name'], email=node['email'])

    def list_pickup_issues(
        self,
        team_id: str,
        state_names: list[str],
        *,
        exclude_labels: list[str] | None = None,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[Issue]:
        """Return unclaimed issues in `state_names` without any `exclude_labels`.

        "Unclaimed" = `assignee = null`. When `project_id` is given,
        results are scoped to that Linear project — workers running with
        `--project foo` never pick up issues created in another profile.
        Sorted by Linear's default order (manual + priority). Limit
        defaults to 50, which is plenty for a single-team POC.
        """
        document = (
            'query Pickup($filter: IssueFilter!, $first: Int!) { '
            'issues(filter: $filter, first: $first) { nodes {'
            + ISSUE_FRAGMENT
            + '} } }'
        )
        filt: dict[str, Any] = {
            'team': {'id': {'eq': team_id}},
            'state': {'name': {'in': state_names}},
            'assignee': {'null': True},
        }
        if project_id is not None:
            filt['project'] = {'id': {'eq': project_id}}
        if exclude_labels:
            filt['labels'] = {'name': {'nin': exclude_labels}}
        data = self.query(document, {'filter': filt, 'first': limit})
        return [issue_from_node(node) for node in data['issues']['nodes']]

    def list_active_issues(
        self,
        team_id: str,
        *,
        project_id: str | None = None,
        limit: int = 200,
    ) -> list[Issue]:
        """All non-canceled issues in the team/project, regardless of assignee.

        Used by the TUI queue panel to count issues per workflow state. Does
        not filter on assignee or labels — the panel wants the full picture
        across the workflow (Triage / Backlog / In Progress / Review /
        Awaiting approval / Done), not just pickup-eligible work.
        """
        document = (
            'query Active($filter: IssueFilter!, $first: Int!) { '
            'issues(filter: $filter, first: $first) { nodes {'
            + ISSUE_FRAGMENT
            + '} } }'
        )
        filt: dict[str, Any] = {
            'team': {'id': {'eq': team_id}},
            'state': {'type': {'neq': 'canceled'}},
        }
        if project_id is not None:
            filt['project'] = {'id': {'eq': project_id}}
        data = self.query(document, {'filter': filt, 'first': limit})
        return [issue_from_node(node) for node in data['issues']['nodes']]

    def list_assigned_issues(
        self,
        agent_user_id: str,
        *,
        project_id: str | None = None,
    ) -> list[Issue]:
        """Return issues currently assigned to a specific user.

        Used by the stale-claim recovery: if a worker died, its issues sit
        with the agent user as assignee but no heartbeat — recovery un-claims
        them so they're reachable again. Pass `project_id` to keep
        recovery scoped to the active profile (other profiles' stale
        claims are someone else's problem).
        """
        if project_id is None:
            document = (
                'query Assigned($userId: String!) { '
                'user(id: $userId) { assignedIssues(first: 100) { nodes {'
                + ISSUE_FRAGMENT
                + '} } } }'
            )
            data = self.query(document, {'userId': agent_user_id})
            nodes = data['user']['assignedIssues']['nodes']
            return [issue_from_node(node) for node in nodes]
        document = (
            'query Assigned($filter: IssueFilter!) { '
            'issues(filter: $filter, first: 100) { nodes {' + ISSUE_FRAGMENT + '} } }'
        )
        filt: dict[str, Any] = {
            'assignee': {'id': {'eq': agent_user_id}},
            'project': {'id': {'eq': project_id}},
        }
        data = self.query(document, {'filter': filt})
        return [issue_from_node(node) for node in data['issues']['nodes']]

    def create_issue(
        self,
        *,
        team_id: str,
        title: str,
        description: str,
        parent_id: str | None = None,
        label_ids: tuple[str, ...] = (),
        estimate: int | None = None,
        state_id: str | None = None,
        project_id: str | None = None,
    ) -> Issue:
        """Create a new issue and return it.

        Used by ARCHITECT to spawn child issues during decomposition.
        `project_id` ties the new issue to the active Linear project so
        children stay co-located with their parent under multi-project
        configs.
        """
        input_payload: dict[str, Any] = {
            'teamId': team_id,
            'title': title,
            'description': description,
        }
        if parent_id is not None:
            input_payload['parentId'] = parent_id
        if label_ids:
            input_payload['labelIds'] = list(label_ids)
        if estimate is not None:
            input_payload['estimate'] = estimate
        if state_id is not None:
            input_payload['stateId'] = state_id
        if project_id is not None:
            input_payload['projectId'] = project_id

        document = (
            'mutation IssueCreate($input: IssueCreateInput!) { '
            'issueCreate(input: $input) { success issue {' + ISSUE_FRAGMENT + '} } }'
        )
        data = self.query(document, {'input': input_payload})
        result = data['issueCreate']
        if not result.get('success'):
            raise LinearError(f'Failed to create issue {title!r}')
        return issue_from_node(result['issue'])

    def list_children(self, parent_id: str) -> list[Issue]:
        """Return all non-archived children of a parent issue."""
        document = (
            'query Children($parentId: ID!) { '
            'issues(filter: {parent: {id: {eq: $parentId}}}, first: 50) '
            '{ nodes {' + ISSUE_FRAGMENT + '} } }'
        )
        data = self.query(document, {'parentId': parent_id})
        return [issue_from_node(node) for node in data['issues']['nodes']]

    def create_issue_relation(
        self,
        issue_id: str,
        related_issue_id: str,
        *,
        relation_type: str = 'blocks',
    ) -> None:
        """Create a Linear issue relation (default `blocks`).

        Used by ARCHITECT to encode `depends_on` between sibling children:
        the dependent child gets a `blockedBy` relation pointing at the
        blocker. Workers filter such issues out of the candidate pool until
        the blocker reaches a completed/canceled state.
        """
        document = """
            mutation IssueRelationCreate($input: IssueRelationCreateInput!) {
              issueRelationCreate(input: $input) {
                success
              }
            }
        """
        data = self.query(
            document,
            {
                'input': {
                    'issueId': issue_id,
                    'relatedIssueId': related_issue_id,
                    'type': relation_type,
                }
            },
        )
        if not data['issueRelationCreate'].get('success'):
            raise LinearError(
                f'Failed to create {relation_type} relation '
                f'{issue_id!r} -> {related_issue_id!r}'
            )

    def archive_issue(self, issue_id: str) -> None:
        """Archive an issue (used when rebuilding a rejected decomposition)."""
        document = """
            mutation IssueArchive($id: String!) {
              issueArchive(id: $id) {
                success
              }
            }
        """
        data = self.query(document, {'id': issue_id})
        if not data['issueArchive'].get('success'):
            raise LinearError(f'Failed to archive issue {issue_id!r}')

    def list_comments(self, issue_id: str) -> list[Comment]:
        """Fetch comments on an issue, oldest first by `createdAt`.

        Linear's default `comments(first: N)` ordering is newest-first,
        so we sort by `createdAt` ascending here to match the docstring
        (and what callers like `format_user_comments_block` and
        `find_pr_url_in_comments` rely on).

        `issue_id` may be a UUID or shorthand identifier (`AI-7`).
        """
        document = """
            query Comments($id: String!) {
              issue(id: $id) {
                comments(first: 100) {
                  nodes {
                    id
                    body
                    createdAt
                    user { id }
                    attachments { nodes { id url title subtitle } }
                  }
                }
              }
            }
        """
        data = self.query(document, {'id': issue_id})
        node = data.get('issue')
        if not node:
            raise LinearError(f'Linear issue {issue_id!r} not found')
        comments = [
            Comment(
                id=c['id'],
                body=c['body'],
                author_id=_nested_id(c.get('user')),
                created_at=c.get('createdAt') or '',
                attachments=_attachments_from_node(c),
            )
            for c in node['comments']['nodes']
        ]
        comments.sort(key=lambda c: c.created_at)
        return comments

    def add_comment(self, issue_id: str, body: str) -> str:
        """Post a comment, returning its UUID."""
        document = """
            mutation Comment($input: CommentCreateInput!) {
              commentCreate(input: $input) {
                success
                comment { id }
              }
            }
        """
        variables = {'input': {'issueId': issue_id, 'body': body}}
        data = self.query(document, variables)
        result = data['commentCreate']
        if not result.get('success'):
            raise LinearError(f'Failed to add comment on issue {issue_id!r}')
        return result['comment']['id']
