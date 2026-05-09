"""Unit tests for the in-tree GitHub MCP proxy."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from albedo import mcp_github_proxy
from albedo.mcp_audit import audit_log_path
from albedo.mcp_github_proxy import (
    GITHUB_PAT_ENV,
    PROTOCOL_VERSION,
    TOOL_REGISTRY,
    PatNotFoundError,
    ProxyConfig,
    handle_message,
    handle_tool_call,
    load_pat_from_env_file,
    main,
    serve,
)

_DUMMY_PAT = 'ghp_test_token'
_OWNER = 'octocat'
_REPO = 'sample'

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def _isolate_pat_env(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure tests never inherit a real PAT from the operator's shell."""
    monkeypatch.delenv(GITHUB_PAT_ENV, raising=False)


def _make_config(state_dir: Path) -> ProxyConfig:
    return ProxyConfig(
        pat=_DUMMY_PAT,
        allowed_owner=_OWNER,
        allowed_repo=_REPO,
        state_dir=state_dir,
        issue_id='AI-72',
        agent_id='3',
    )


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={'ok': True})


def _make_recording_transport(
    handler: Handler | None = None,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """Build a `MockTransport` that records every outbound request."""
    seen: list[httpx.Request] = []
    real_handler: Handler = handler if handler is not None else _ok

    def _capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return real_handler(request)

    return httpx.MockTransport(_capture), seen


@contextmanager
def _open_client(
    config: ProxyConfig, transport: httpx.BaseTransport
) -> Generator[httpx.Client]:
    client = mcp_github_proxy._make_client(  # pyright: ignore[reportPrivateUsage]
        config.pat, base_url=config.base_url, transport=transport
    )
    try:
        yield client
    finally:
        client.close()


def _result(response: dict[str, Any]) -> dict[str, Any]:
    """Extract the JSON-RPC result envelope, asserting it isn't an error."""
    assert 'result' in response, response
    res: Any = response['result']
    assert isinstance(res, dict)
    return cast('dict[str, Any]', res)


def _content_text(envelope: dict[str, Any]) -> str:
    content_raw: Any = envelope['content']
    assert isinstance(content_raw, list) and content_raw
    content = cast('list[dict[str, Any]]', content_raw)
    text: Any = content[0]['text']
    assert isinstance(text, str)
    return text


# ---------------------------------------------------------------- PAT loader


def test_load_pat_from_env_file_reads_token(tmp_path: Path) -> None:
    env_file = tmp_path / '.env'
    env_file.write_text(
        f'OTHER=foo\n{GITHUB_PAT_ENV}=ghp_real_value\n', encoding='utf-8'
    )
    assert load_pat_from_env_file(env_file) == 'ghp_real_value'


def test_load_pat_strips_quotes(tmp_path: Path) -> None:
    env_file = tmp_path / '.env'
    env_file.write_text(f'{GITHUB_PAT_ENV}="quoted-token"\n', encoding='utf-8')
    assert load_pat_from_env_file(env_file) == 'quoted-token'


def test_load_pat_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(PatNotFoundError, match='not found'):
        load_pat_from_env_file(tmp_path / '.env')


def test_load_pat_missing_key_raises(tmp_path: Path) -> None:
    env_file = tmp_path / '.env'
    env_file.write_text('OTHER=foo\n', encoding='utf-8')
    with pytest.raises(PatNotFoundError, match='missing or empty'):
        load_pat_from_env_file(env_file)


def test_load_pat_empty_value_raises(tmp_path: Path) -> None:
    env_file = tmp_path / '.env'
    env_file.write_text(f'{GITHUB_PAT_ENV}=\n', encoding='utf-8')
    with pytest.raises(PatNotFoundError, match='missing or empty'):
        load_pat_from_env_file(env_file)


def test_load_pat_ignores_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if process env has a PAT, the file is the only source."""
    monkeypatch.setenv(GITHUB_PAT_ENV, 'leaked-from-process-env')
    env_file = tmp_path / '.env'
    env_file.write_text(f'{GITHUB_PAT_ENV}=from-file\n', encoding='utf-8')
    assert load_pat_from_env_file(env_file) == 'from-file'


# ---------------------------------------------------- Tool registry surface


def test_tool_registry_covers_documented_inventory() -> None:
    names = {spec.name for spec in TOOL_REGISTRY}
    expected = {
        'create_pull_request',
        'get_pull_request',
        'list_pull_requests',
        'get_pull_request_files',
        'get_pull_request_reviews',
        'get_pull_request_comments',
        'get_pull_request_status',
        'create_pull_request_review',
        'add_issue_comment',
        'list_workflow_runs',
        'list_workflow_jobs',
        'get_job_logs',
        'rerun_workflow_run',
    }
    assert expected.issubset(names), expected - names


def test_tools_list_returns_full_inventory(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    transport, _ = _make_recording_transport()
    with _open_client(config, transport) as client:
        response = handle_message(
            message={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
            config=config,
            client=client,
        )
    assert response is not None
    result = _result(response)
    tools_any: Any = result['tools']
    assert isinstance(tools_any, list)
    tools = cast('list[dict[str, Any]]', tools_any)
    assert len(tools) == len(TOOL_REGISTRY)
    for entry in tools:
        assert {'name', 'description', 'inputSchema'}.issubset(entry)


def test_initialize_advertises_tools_capability(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    transport, _ = _make_recording_transport()
    with _open_client(config, transport) as client:
        response = handle_message(
            message={'jsonrpc': '2.0', 'id': 0, 'method': 'initialize'},
            config=config,
            client=client,
        )
    assert response is not None
    result = _result(response)
    assert result['protocolVersion'] == PROTOCOL_VERSION
    capabilities: Any = result['capabilities']
    assert isinstance(capabilities, dict)
    assert 'tools' in capabilities


# --------------------------------------------------------------- Repo gate


def test_repo_gate_blocks_mismatched_owner_no_outbound_call(
    tmp_path: Path,
) -> None:
    """The flagship AC: mismatched owner/repo → structured error AND zero traffic."""
    config = _make_config(tmp_path)
    transport, seen = _make_recording_transport()

    with _open_client(config, transport) as client:
        response = handle_tool_call(
            request_id=1,
            params={
                'name': 'create_pull_request',
                'arguments': {
                    'owner': 'attacker',
                    'repo': _REPO,
                    'title': 'pwn',
                    'head': 'feature',
                    'base': 'main',
                },
            },
            config=config,
            client=client,
        )

    assert seen == [], (
        f'repo gate must not produce outbound traffic; got {len(seen)} requests'
    )
    envelope = _result(response)
    assert envelope['isError'] is True
    text = _content_text(envelope)
    assert 'repo gate refused' in text
    assert 'attacker' in text
    audit_lines = audit_log_path(tmp_path).read_text('utf-8').splitlines()
    assert len(audit_lines) == 1
    entry: dict[str, Any] = json.loads(audit_lines[0])
    assert entry['outcome'] == 'refused'
    assert entry['tool'] == 'github.create_pull_request'


def test_repo_gate_blocks_mismatched_repo(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    transport, seen = _make_recording_transport()
    with _open_client(config, transport) as client:
        response = handle_tool_call(
            request_id=2,
            params={
                'name': 'list_pull_requests',
                'arguments': {'owner': _OWNER, 'repo': 'other-repo'},
            },
            config=config,
            client=client,
        )
    assert seen == []
    assert _result(response)['isError'] is True


def test_repo_gate_rejects_missing_owner_repo(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    transport, seen = _make_recording_transport()
    with _open_client(config, transport) as client:
        response = handle_tool_call(
            request_id=3,
            params={
                'name': 'list_pull_requests',
                'arguments': {'state': 'open'},
            },
            config=config,
            client=client,
        )
    assert seen == []
    envelope = _result(response)
    assert envelope['isError'] is True
    assert 'missing owner/repo' in _content_text(envelope)


# ---------------------------------------------------------- Happy path call


def test_create_pull_request_happy_path(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == 'POST'
        assert request.url.path == f'/repos/{_OWNER}/{_REPO}/pulls'
        body: dict[str, Any] = json.loads(request.content.decode('utf-8'))
        assert body == {
            'title': 'feat',
            'head': 'task/x',
            'base': 'main',
            'body': 'desc',
        }
        assert request.headers['Authorization'] == f'Bearer {_DUMMY_PAT}'
        return httpx.Response(
            201,
            json={
                'number': 42,
                'html_url': 'https://github.com/octocat/sample/pull/42',
            },
        )

    transport, seen = _make_recording_transport(handler)
    with _open_client(config, transport) as client:
        response = handle_tool_call(
            request_id=1,
            params={
                'name': 'create_pull_request',
                'arguments': {
                    'owner': _OWNER,
                    'repo': _REPO,
                    'title': 'feat',
                    'head': 'task/x',
                    'base': 'main',
                    'body': 'desc',
                },
            },
            config=config,
            client=client,
        )

    assert len(seen) == 1
    envelope = _result(response)
    assert envelope['isError'] is False
    assert 'pull/42' in _content_text(envelope)
    audit_lines = audit_log_path(tmp_path).read_text('utf-8').splitlines()
    assert len(audit_lines) == 1
    entry: dict[str, Any] = json.loads(audit_lines[0])
    assert entry['outcome'] == 'ok'
    assert entry['tool'] == 'github.create_pull_request'


def test_upstream_error_records_error_outcome(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={'message': 'Validation Failed'})

    transport, _ = _make_recording_transport(handler)
    with _open_client(config, transport) as client:
        response = handle_tool_call(
            request_id=1,
            params={
                'name': 'create_pull_request',
                'arguments': {
                    'owner': _OWNER,
                    'repo': _REPO,
                    'title': '',
                    'head': 'x',
                    'base': 'main',
                },
            },
            config=config,
            client=client,
        )
    envelope = _result(response)
    assert envelope['isError'] is True
    assert 'github HTTP 422' in _content_text(envelope)
    entry: dict[str, Any] = json.loads(audit_log_path(tmp_path).read_text('utf-8'))
    assert entry['outcome'] == 'error'


def test_unknown_tool_returns_jsonrpc_method_not_found(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    transport, seen = _make_recording_transport()
    with _open_client(config, transport) as client:
        response = handle_tool_call(
            request_id=7,
            params={'name': 'delete_repo', 'arguments': {}},
            config=config,
            client=client,
        )
    assert seen == []
    assert 'error' in response
    error: Any = response['error']
    assert isinstance(error, dict)
    assert error['code'] == -32601
    audit_lines = audit_log_path(tmp_path).read_text('utf-8').splitlines()
    assert len(audit_lines) == 1
    entry: dict[str, Any] = json.loads(audit_lines[0])
    assert entry['outcome'] == 'refused'
    assert entry['tool'] == 'github.delete_repo'


def test_get_job_logs_follows_redirect_to_log_storage(tmp_path: Path) -> None:
    """GitHub returns 302 → S3 for job logs; the proxy must follow it."""
    config = _make_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == 'api.github.com' or request.url.path.startswith(
            f'/repos/{_OWNER}/{_REPO}/actions/jobs/'
        ):
            return httpx.Response(
                302,
                headers={
                    'Location': 'https://logs.example.com/blob?token=abc',
                },
            )
        assert request.url.host == 'logs.example.com'
        return httpx.Response(200, text='log line one\nlog line two\n')

    transport, seen = _make_recording_transport(handler)
    with _open_client(config, transport) as client:
        response = handle_tool_call(
            request_id=1,
            params={
                'name': 'get_job_logs',
                'arguments': {
                    'owner': _OWNER,
                    'repo': _REPO,
                    'job_id': 99,
                },
            },
            config=config,
            client=client,
        )

    assert len(seen) == 2, 'expected redirect to be followed'
    envelope = _result(response)
    assert envelope['isError'] is False
    assert 'log line one' in _content_text(envelope)


def test_transport_failure_records_error_outcome(tmp_path: Path) -> None:
    """An httpx.HTTPError surfaces as a structured tool error + audit row."""
    config = _make_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('connection refused', request=request)

    transport = httpx.MockTransport(handler)
    with _open_client(config, transport) as client:
        response = handle_tool_call(
            request_id=1,
            params={
                'name': 'list_pull_requests',
                'arguments': {'owner': _OWNER, 'repo': _REPO},
            },
            config=config,
            client=client,
        )
    envelope = _result(response)
    assert envelope['isError'] is True
    assert 'github transport error' in _content_text(envelope)
    entry: dict[str, Any] = json.loads(audit_log_path(tmp_path).read_text('utf-8'))
    assert entry['outcome'] == 'error'
    assert entry['tool'] == 'github.list_pull_requests'


# ----------------------------------------------------- Stdio loop / serve


def test_serve_responds_to_initialize_then_tools_list(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    transport, _ = _make_recording_transport()
    stdin = io.StringIO(
        json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})
        + '\n'
        + json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        + '\n'
        + json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        + '\n'
    )
    stdout = io.StringIO()
    serve(config=config, stdin=stdin, stdout=stdout, transport=transport)
    lines = stdout.getvalue().strip().splitlines()
    assert len(lines) == 2
    init_resp: dict[str, Any] = json.loads(lines[0])
    list_resp: dict[str, Any] = json.loads(lines[1])
    assert init_resp['id'] == 1
    assert list_resp['id'] == 2
    list_result: Any = list_resp['result']
    assert isinstance(list_result, dict)
    list_tools: Any = cast('dict[str, Any]', list_result)['tools']
    assert isinstance(list_tools, list)
    assert len(cast('list[Any]', list_tools)) == len(TOOL_REGISTRY)


def test_serve_skips_blank_lines_and_reports_parse_errors(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    transport, _ = _make_recording_transport()
    stdin = io.StringIO('\n\nnot-json\n')
    stdout = io.StringIO()
    serve(config=config, stdin=stdin, stdout=stdout, transport=transport)
    lines = [line for line in stdout.getvalue().splitlines() if line]
    assert len(lines) == 1
    err: dict[str, Any] = json.loads(lines[0])
    error: Any = err['error']
    assert isinstance(error, dict)
    assert error['code'] == -32700


# -------------------------------------------------- Process entry / main()


def _write_repo_with_manifest(
    repo_root: Path, *, owner: str = _OWNER, repo: str = _REPO
) -> None:
    repo_root.mkdir(parents=True, exist_ok=True)
    manifest = (
        'name: sample\n'
        'linear:\n'
        '  project: SampleProj\n'
        'repo:\n'
        '  base_branch: main\n'
        '  github:\n'
        f'    owner: {owner}\n'
        f'    repo: {repo}\n'
    )
    (repo_root / '.albedo.yaml').write_text(manifest, encoding='utf-8')


def test_main_exits_nonzero_when_pat_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Negative-path AC: no PAT in $ALBEDO_HOME/.env → non-zero with stderr."""
    home = tmp_path / 'albedo-home'
    home.mkdir()
    monkeypatch.setenv('ALBEDO_HOME', str(home))
    monkeypatch.setenv(GITHUB_PAT_ENV, 'leaked')
    repo_root = tmp_path / 'repo'
    _write_repo_with_manifest(repo_root)

    code = main(
        [
            '--repo-root',
            str(repo_root),
            '--state-dir',
            str(tmp_path / 'state'),
            '--issue-id',
            'AI-72',
            '--agent-id',
            '3',
        ]
    )
    assert code != 0
    captured = capsys.readouterr()
    assert 'mcp-github-proxy' in captured.err
    assert '.env' in captured.err
    assert os.environ.get(GITHUB_PAT_ENV) is None


def test_main_exits_nonzero_when_pat_key_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / 'albedo-home'
    home.mkdir()
    (home / '.env').write_text(f'{GITHUB_PAT_ENV}=\n', encoding='utf-8')
    monkeypatch.setenv('ALBEDO_HOME', str(home))
    repo_root = tmp_path / 'repo'
    _write_repo_with_manifest(repo_root)
    code = main(
        [
            '--repo-root',
            str(repo_root),
            '--state-dir',
            str(tmp_path / 'state'),
            '--issue-id',
            'AI-72',
            '--agent-id',
            '3',
        ]
    )
    assert code != 0
    assert 'missing or empty' in capsys.readouterr().err


def test_main_fails_clearly_when_manifest_missing_github(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / 'albedo-home'
    home.mkdir()
    (home / '.env').write_text(f'{GITHUB_PAT_ENV}=ghp_value\n', encoding='utf-8')
    monkeypatch.setenv('ALBEDO_HOME', str(home))
    repo_root = tmp_path / 'repo'
    repo_root.mkdir()
    (repo_root / '.albedo.yaml').write_text(
        'name: x\nlinear:\n  project: P\n', encoding='utf-8'
    )
    code = main(
        [
            '--repo-root',
            str(repo_root),
            '--state-dir',
            str(tmp_path / 'state'),
            '--issue-id',
            'AI-72',
            '--agent-id',
            '3',
        ]
    )
    assert code != 0
    assert 'repo.github' in capsys.readouterr().err
