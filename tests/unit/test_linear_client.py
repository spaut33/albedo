"""Tests for the Linear GraphQL client.

We use httpx.MockTransport to assert request shape and exercise retry/error
paths without hitting the real API.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from albedo.linear_client import (
    Issue,
    IssueLabel,
    IssueUpdate,
    LinearClient,
    LinearError,
    RawAttachment,
    Team,
    Viewer,
    WorkflowState,
)

API_URL = 'https://api.linear.app/graphql'


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_retries: int = 3,
) -> LinearClient:
    transport = httpx.MockTransport(handler)
    return LinearClient(
        API_URL,
        SecretStr('lin_api_test'),
        transport=transport,
        max_retries=max_retries,
        backoff_seconds=0.0,
    )


def _ok(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={'data': payload})


def test_query_sends_authorization_header_and_returns_data() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _ok({'foo': 'bar'})

    with _make_client(handler) as client:
        result = client.query('query { foo }', {'x': 1})

    assert result == {'foo': 'bar'}
    assert len(captured) == 1
    assert captured[0].headers['authorization'] == 'lin_api_test'
    assert captured[0].headers['content-type'] == 'application/json'
    body = captured[0].read()
    assert b'"x":1' in body


def test_query_raises_on_graphql_errors() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={'errors': [{'message': 'nope'}]})

    with _make_client(handler) as client, pytest.raises(LinearError, match='nope'):
        client.query('query { foo }')


def test_query_raises_on_missing_data_field() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with _make_client(handler) as client, pytest.raises(LinearError, match='missing'):
        client.query('query { foo }')


def test_query_raises_on_4xx() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text='unauthorized')

    with _make_client(handler) as client, pytest.raises(LinearError, match='401'):
        client.query('query { foo }')


def test_query_retries_on_retryable_status() -> None:
    calls = {'n': 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls['n'] += 1
        if calls['n'] < 3:
            return httpx.Response(503, text='busy')
        return _ok({'foo': 'bar'})

    with _make_client(handler) as client:
        result = client.query('query { foo }')

    assert result == {'foo': 'bar'}
    assert calls['n'] == 3


def test_query_gives_up_after_max_retries() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text='busy')

    with (
        _make_client(handler, max_retries=2) as client,
        pytest.raises(LinearError, match='after 2 attempts'),
    ):
        client.query('query { foo }')


def test_query_retries_on_transport_error() -> None:
    calls = {'n': 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls['n'] += 1
        if calls['n'] < 2:
            raise httpx.ConnectError('boom')
        return _ok({'foo': 'bar'})

    with _make_client(handler) as client:
        assert client.query('query { foo }') == {'foo': 'bar'}
    assert calls['n'] == 2


def test_get_team_by_key_returns_team() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok(
            {
                'teams': {
                    'nodes': [
                        {'id': 't1', 'key': 'ORC', 'name': 'Orchestrator'},
                    ]
                }
            }
        )

    with _make_client(handler) as client:
        team = client.get_team_by_key('ORC')

    assert team == Team(id='t1', key='ORC', name='Orchestrator')


def test_get_team_by_key_raises_when_missing() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok({'teams': {'nodes': []}})

    with (
        _make_client(handler) as client,
        pytest.raises(LinearError, match='not found'),
    ):
        client.get_team_by_key('ORC')


def test_get_team_by_name_returns_team() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok(
            {
                'teams': {
                    'nodes': [{'id': 't1', 'key': 'AIT', 'name': 'AI-Team'}],
                }
            }
        )

    with _make_client(handler) as client:
        team = client.get_team_by_name('AI-Team')

    assert team == Team(id='t1', key='AIT', name='AI-Team')


def test_get_team_by_name_raises_when_missing() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok({'teams': {'nodes': []}})

    with (
        _make_client(handler) as client,
        pytest.raises(LinearError, match='not found'),
    ):
        client.get_team_by_name('AI-Team')


def test_resolve_team_falls_back_to_name_when_key_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode('utf-8')
        if 'TeamByKey' in body:
            return _ok({'teams': {'nodes': []}})
        if 'TeamByName' in body:
            return _ok(
                {
                    'teams': {
                        'nodes': [{'id': 't1', 'key': 'AIT', 'name': 'AI-Team'}],
                    }
                }
            )
        raise AssertionError(f'Unexpected request: {body[:80]}')

    with _make_client(handler) as client:
        team = client.resolve_team('AI-Team')

    assert team.name == 'AI-Team'


def test_resolve_team_returns_key_match_first() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode('utf-8')
        if 'TeamByKey' in body:
            return _ok(
                {'teams': {'nodes': [{'id': 't1', 'key': 'ORC', 'name': 'Other'}]}}
            )
        raise AssertionError(f'Should not call TeamByName when key found: {body}')

    with _make_client(handler) as client:
        team = client.resolve_team('ORC')

    assert team.key == 'ORC'


def test_list_workflow_states_returns_states() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok(
            {
                'workflowStates': {
                    'nodes': [
                        {'id': 's1', 'name': 'Backlog', 'type': 'backlog'},
                        {'id': 's2', 'name': 'Done', 'type': 'completed'},
                    ]
                }
            }
        )

    with _make_client(handler) as client:
        states = client.list_workflow_states('t1')

    assert states == [
        WorkflowState(id='s1', name='Backlog', type='backlog'),
        WorkflowState(id='s2', name='Done', type='completed'),
    ]


def test_create_workflow_state_returns_created_state() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok(
            {
                'workflowStateCreate': {
                    'success': True,
                    'workflowState': {
                        'id': 's3',
                        'name': 'Review',
                        'type': 'started',
                    },
                }
            }
        )

    with _make_client(handler) as client:
        state = client.create_workflow_state('t1', 'Review', 'started')

    assert state == WorkflowState(id='s3', name='Review', type='started')


def test_create_workflow_state_raises_when_unsuccessful() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok(
            {
                'workflowStateCreate': {
                    'success': False,
                    'workflowState': None,
                }
            }
        )

    with (
        _make_client(handler) as client,
        pytest.raises(LinearError, match='Failed to create workflow state'),
    ):
        client.create_workflow_state('t1', 'Review', 'started')


def test_list_issue_labels_returns_labels() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok(
            {
                'issueLabels': {
                    'nodes': [
                        {'id': 'l1', 'name': 'draft'},
                        {'id': 'l2', 'name': 'stuck'},
                    ]
                }
            }
        )

    with _make_client(handler) as client:
        labels = client.list_issue_labels('t1')

    assert labels == [
        IssueLabel(id='l1', name='draft'),
        IssueLabel(id='l2', name='stuck'),
    ]


def test_create_issue_label_returns_created_label() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok(
            {
                'issueLabelCreate': {
                    'success': True,
                    'issueLabel': {'id': 'l3', 'name': 'kind:final-pr'},
                }
            }
        )

    with _make_client(handler) as client:
        label = client.create_issue_label('t1', 'kind:final-pr')

    assert label == IssueLabel(id='l3', name='kind:final-pr')


def test_create_issue_label_raises_when_unsuccessful() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok(
            {
                'issueLabelCreate': {
                    'success': False,
                    'issueLabel': None,
                }
            }
        )

    with (
        _make_client(handler) as client,
        pytest.raises(LinearError, match='Failed to create issue label'),
    ):
        client.create_issue_label('t1', 'kind:final-pr')


def _issue_node(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        'id': 'uuid-1',
        'identifier': 'AI-5',
        'title': 'Add filter',
        'description': 'AC: ...',
        'url': 'https://linear.app/acme/issue/AI-5/add-filter',
        'branchName': 'roman/ai-5-add-filter',
        'state': {'id': 'state-backlog', 'name': 'Backlog'},
        'assignee': None,
        'parent': None,
        'labels': {'nodes': []},
    }
    base.update(overrides)
    return base


def test_get_issue_returns_full_issue() -> None:
    node = _issue_node(
        assignee={'id': 'user-1'},
        parent={'id': 'uuid-parent'},
        labels={
            'nodes': [
                {'id': 'lab-1', 'name': 'size:M'},
                {'id': 'lab-2', 'name': 'attempts:1'},
            ]
        },
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return _ok({'issue': node})

    with _make_client(handler) as client:
        issue = client.get_issue('AI-5')

    assert issue == Issue(
        id='uuid-1',
        identifier='AI-5',
        title='Add filter',
        description='AC: ...',
        state_id='state-backlog',
        state_name='Backlog',
        assignee_id='user-1',
        label_ids=('lab-1', 'lab-2'),
        label_names=('size:M', 'attempts:1'),
        parent_id='uuid-parent',
        branch_name='roman/ai-5-add-filter',
        url='https://linear.app/acme/issue/AI-5/add-filter',
    )


def test_get_issue_carries_attachments_when_present() -> None:
    node = _issue_node(
        attachments={
            'nodes': [
                {
                    'id': 'att-1',
                    'url': 'https://uploads.linear.app/issue/mock.png',
                    'title': 'mock',
                    'subtitle': '',
                },
                {
                    'id': 'att-2',
                    'url': '',  # dropped
                    'title': 'broken',
                    'subtitle': '',
                },
            ]
        }
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return _ok({'issue': node})

    with _make_client(handler) as client:
        issue = client.get_issue('AI-5')
    assert issue.attachments == (
        RawAttachment(
            id='att-1',
            url='https://uploads.linear.app/issue/mock.png',
            title='mock',
            subtitle='',
        ),
    )


def test_get_issue_handles_null_optional_fields() -> None:
    node = _issue_node(description=None, branchName=None)

    def handler(_: httpx.Request) -> httpx.Response:
        return _ok({'issue': node})

    with _make_client(handler) as client:
        issue = client.get_issue('AI-5')

    assert issue.description == ''
    assert issue.branch_name == ''
    assert issue.assignee_id is None
    assert issue.parent_id is None
    assert issue.label_ids == ()


def test_get_issue_raises_when_missing() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok({'issue': None})

    with (
        _make_client(handler) as client,
        pytest.raises(LinearError, match='not found'),
    ):
        client.get_issue('AI-999')


def test_update_issue_sends_only_set_fields() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode('utf-8')
        captured.append({'body': body})
        return _ok(
            {
                'issueUpdate': {
                    'success': True,
                    'issue': _issue_node(
                        state={'id': 'state-review', 'name': 'Review'}
                    ),
                }
            }
        )

    with _make_client(handler) as client:
        issue = client.update_issue(
            'uuid-1', IssueUpdate(state_id='state-review', label_ids=('lab-1',))
        )

    assert issue.state_name == 'Review'
    assert '"stateId":"state-review"' in captured[0]['body']
    assert '"labelIds":["lab-1"]' in captured[0]['body']
    assert '"assigneeId"' not in captured[0]['body']


def test_update_issue_unset_assignee_sends_null() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read().decode('utf-8'))
        return _ok(
            {
                'issueUpdate': {
                    'success': True,
                    'issue': _issue_node(),
                }
            }
        )

    with _make_client(handler) as client:
        client.update_issue('uuid-1', IssueUpdate(unset_assignee=True))

    assert '"assigneeId":null' in captured[0]


def test_update_issue_assignee_id_takes_priority_over_unset_when_unset_false() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read().decode('utf-8'))
        return _ok(
            {
                'issueUpdate': {
                    'success': True,
                    'issue': _issue_node(assignee={'id': 'user-2'}),
                }
            }
        )

    with _make_client(handler) as client:
        client.update_issue('uuid-1', IssueUpdate(assignee_id='user-2'))

    assert '"assigneeId":"user-2"' in captured[0]


def test_update_issue_description_passes_through() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read().decode('utf-8'))
        return _ok(
            {
                'issueUpdate': {
                    'success': True,
                    'issue': _issue_node(description='new body'),
                }
            }
        )

    with _make_client(handler) as client:
        issue = client.update_issue('uuid-1', IssueUpdate(description='new body'))

    assert issue.description == 'new body'
    assert '"description":"new body"' in captured[0]


def test_update_issue_rejects_empty_update() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError('No request expected')

    with (
        _make_client(handler) as client,
        pytest.raises(LinearError, match='at least one field'),
    ):
        client.update_issue('uuid-1', IssueUpdate())


def test_update_issue_raises_when_unsuccessful() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok({'issueUpdate': {'success': False, 'issue': _issue_node()}})

    with (
        _make_client(handler) as client,
        pytest.raises(LinearError, match='Failed to update issue'),
    ):
        client.update_issue('uuid-1', IssueUpdate(state_id='state-x'))


def test_viewer_returns_user() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok({'viewer': {'id': 'u1', 'name': 'Roman', 'email': 'r@x.com'}})

    with _make_client(handler) as client:
        v = client.viewer()
    assert v == Viewer(id='u1', name='Roman', email='r@x.com')


def test_list_pickup_issues_filters_and_returns_issues() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read().decode('utf-8'))
        return _ok(
            {
                'issues': {
                    'nodes': [
                        _issue_node(),
                        _issue_node(
                            id='uuid-2',
                            identifier='AI-7',
                            assignee={'id': 'someone'},
                        ),
                    ]
                }
            }
        )

    with _make_client(handler) as client:
        issues = client.list_pickup_issues(
            'team-1', ['Backlog'], exclude_labels=['draft']
        )

    assert len(issues) == 2
    assert issues[0].identifier == 'AI-5'
    body = captured[0]
    assert '"team":{"id":{"eq":"team-1"}}' in body
    assert '"state":{"name":{"in":["Backlog"]}}' in body
    assert '"assignee":{"null":true}' in body
    assert '"labels":{"name":{"nin":["draft"]}}' in body


def test_list_pickup_issues_omits_label_filter_when_empty() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read().decode('utf-8'))
        return _ok({'issues': {'nodes': []}})

    with _make_client(handler) as client:
        client.list_pickup_issues('team-1', ['Backlog'])

    assert '"labels"' not in captured[0]


def test_list_assigned_issues_returns_issues() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok(
            {
                'user': {
                    'assignedIssues': {
                        'nodes': [
                            _issue_node(assignee={'id': 'user-1'}),
                        ]
                    }
                }
            }
        )

    with _make_client(handler) as client:
        issues = client.list_assigned_issues('user-1')
    assert len(issues) == 1
    assert issues[0].assignee_id == 'user-1'


def test_list_comments_returns_comments_oldest_first() -> None:
    """Linear's default ordering is newest-first; list_comments must
    sort by createdAt ascending so callers (`_latest_id`,
    `format_user_comments_block`, `find_pr_url_in_comments`) get a
    stable oldest-first stream regardless of API quirks."""

    def handler(_: httpx.Request) -> httpx.Response:
        return _ok(
            {
                'issue': {
                    'comments': {
                        'nodes': [
                            {
                                'id': 'c-newest',
                                'body': 'let us cancel this',
                                'createdAt': '2026-05-02T14:47:00.000Z',
                                'user': {'id': 'u1'},
                            },
                            {
                                'id': 'c-mid',
                                'body': '**agent-3**: BLOCKED: ...',
                                'createdAt': '2026-05-02T13:15:00.000Z',
                                'user': {'id': 'bot'},
                            },
                            {
                                'id': 'c-oldest',
                                'body': 'i was wrong',
                                'createdAt': '2026-05-02T13:14:00.000Z',
                                'user': {'id': 'u1'},
                            },
                        ]
                    }
                }
            }
        )

    with _make_client(handler) as client:
        comments = client.list_comments('AI-47')

    assert [c.id for c in comments] == ['c-oldest', 'c-mid', 'c-newest']
    assert comments[-1].body == 'let us cancel this'


def test_list_comments_carries_attachments_when_present() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok(
            {
                'issue': {
                    'comments': {
                        'nodes': [
                            {
                                'id': 'c1',
                                'body': 'see attached',
                                'createdAt': '2026-05-02T10:00:00.000Z',
                                'user': {'id': 'u1'},
                                'attachments': {
                                    'nodes': [
                                        {
                                            'id': 'att-1',
                                            'url': 'https://uploads.linear.app/foo/spec.pdf',
                                            'title': 'spec',
                                            'subtitle': 'v2',
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                }
            }
        )

    with _make_client(handler) as client:
        comments = client.list_comments('AI-47')
    assert comments[0].attachments == (
        RawAttachment(
            id='att-1',
            url='https://uploads.linear.app/foo/spec.pdf',
            title='spec',
            subtitle='v2',
        ),
    )


def test_list_comments_raises_when_issue_missing() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok({'issue': None})

    with (
        _make_client(handler) as client,
        pytest.raises(LinearError, match='not found'),
    ):
        client.list_comments('AI-999')


def test_create_issue_returns_issue_with_labels_and_parent() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read().decode('utf-8'))
        return _ok(
            {
                'issueCreate': {
                    'success': True,
                    'issue': _issue_node(
                        id='uuid-c',
                        identifier='AI-11',
                        parent={'id': 'uuid-parent'},
                        labels={'nodes': [{'id': 'lab-draft', 'name': 'draft'}]},
                    ),
                }
            }
        )

    with _make_client(handler) as client:
        issue = client.create_issue(
            team_id='t1',
            title='Add filter',
            description='AC',
            parent_id='uuid-parent',
            label_ids=('lab-draft',),
            estimate=3,
        )

    assert issue.id == 'uuid-c'
    assert issue.identifier == 'AI-11'
    body = captured[0]
    assert '"teamId":"t1"' in body
    assert '"parentId":"uuid-parent"' in body
    assert '"labelIds":["lab-draft"]' in body
    assert '"estimate":3' in body


def test_create_issue_raises_on_unsuccessful() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok({'issueCreate': {'success': False, 'issue': _issue_node()}})

    with (
        _make_client(handler) as client,
        pytest.raises(LinearError, match='Failed to create issue'),
    ):
        client.create_issue(team_id='t1', title='X', description='Y')


def test_list_children_returns_issues() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok(
            {
                'issues': {
                    'nodes': [
                        _issue_node(
                            id='uuid-c1',
                            identifier='AI-11',
                            parent={'id': 'uuid-parent'},
                        ),
                        _issue_node(
                            id='uuid-c2',
                            identifier='AI-12',
                            parent={'id': 'uuid-parent'},
                        ),
                    ]
                }
            }
        )

    with _make_client(handler) as client:
        children = client.list_children('uuid-parent')
    assert [c.identifier for c in children] == ['AI-11', 'AI-12']


def test_archive_issue_succeeds_quietly() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok({'issueArchive': {'success': True}})

    with _make_client(handler) as client:
        client.archive_issue('uuid-1')


def test_archive_issue_raises_on_failure() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok({'issueArchive': {'success': False}})

    with (
        _make_client(handler) as client,
        pytest.raises(LinearError, match='Failed to archive'),
    ):
        client.archive_issue('uuid-1')


def test_add_comment_returns_id() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok(
            {
                'commentCreate': {
                    'success': True,
                    'comment': {'id': 'comment-1'},
                }
            }
        )

    with _make_client(handler) as client:
        comment_id = client.add_comment('uuid-1', 'PR: https://github.com/x/y/pull/3')

    assert comment_id == 'comment-1'


def test_add_comment_raises_when_unsuccessful() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok({'commentCreate': {'success': False, 'comment': None}})

    with (
        _make_client(handler) as client,
        pytest.raises(LinearError, match='Failed to add comment'),
    ):
        client.add_comment('uuid-1', 'hello')


def test_get_project_by_name_returns_team_scoped_project() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok(
            {
                'projects': {
                    'nodes': [
                        {
                            'id': 'p-1',
                            'name': 'Sample',
                            'teams': {'nodes': [{'id': 't-other'}]},
                        },
                        {
                            'id': 'p-2',
                            'name': 'Sample',
                            'teams': {'nodes': [{'id': 't-target'}]},
                        },
                    ]
                }
            }
        )

    with _make_client(handler) as client:
        project = client.get_project_by_name('t-target', 'Sample')

    assert project.id == 'p-2'
    assert project.name == 'Sample'


def test_get_project_by_name_raises_when_team_does_not_match() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _ok(
            {
                'projects': {
                    'nodes': [
                        {
                            'id': 'p-1',
                            'name': 'Sample',
                            'teams': {'nodes': [{'id': 't-other'}]},
                        },
                    ]
                }
            }
        )

    with (
        _make_client(handler) as client,
        pytest.raises(LinearError, match='not found for team'),
    ):
        client.get_project_by_name('t-target', 'Sample')


def test_list_pickup_issues_includes_project_filter() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read().decode('utf-8'))
        return _ok({'issues': {'nodes': []}})

    with _make_client(handler) as client:
        client.list_pickup_issues(
            't-1', ['Backlog'], exclude_labels=['stuck'], project_id='p-42'
        )

    body = captured[0]
    assert '"project"' in body
    assert '"p-42"' in body


def test_list_active_issues_filters_team_and_excludes_canceled() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read().decode('utf-8'))
        return _ok(
            {
                'issues': {
                    'nodes': [
                        _issue_node(),
                        _issue_node(id='uuid-2', identifier='AI-9'),
                    ]
                }
            }
        )

    with _make_client(handler) as client:
        issues = client.list_active_issues('team-1', project_id='p-42')

    assert len(issues) == 2
    assert {i.identifier for i in issues} == {'AI-5', 'AI-9'}
    body = captured[0]
    assert '"team":{"id":{"eq":"team-1"}}' in body
    assert '"state":{"type":{"neq":"canceled"}}' in body
    assert '"project":{"id":{"eq":"p-42"}}' in body
    # No assignee filter — panel wants the full picture.
    assert '"assignee"' not in body


def test_list_active_issues_omits_project_when_unset() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read().decode('utf-8'))
        return _ok({'issues': {'nodes': []}})

    with _make_client(handler) as client:
        client.list_active_issues('team-1')

    assert '"project"' not in captured[0]


def test_create_issue_passes_project_id() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read().decode('utf-8'))
        return _ok(
            {
                'issueCreate': {
                    'success': True,
                    'issue': {
                        'id': 'uuid-1',
                        'identifier': 'AI-9',
                        'title': 'X',
                        'description': '',
                        'state': {'id': 's', 'name': 'Triage'},
                        'assignee': None,
                        'parent': None,
                        'labels': {'nodes': []},
                        'branchName': '',
                        'inverseRelations': {'nodes': []},
                    },
                }
            }
        )

    with _make_client(handler) as client:
        client.create_issue(
            team_id='t-1',
            title='X',
            description='Y',
            project_id='p-42',
        )

    assert '"projectId"' in captured[0]
    assert '"p-42"' in captured[0]
