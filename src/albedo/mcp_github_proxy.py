"""In-tree MCP proxy that fronts the narrow GitHub surface Albedo uses.

The PAT lives in this child process only — read directly from
`$ALBEDO_HOME/.env` at startup, never inherited from the spawning
claude's environment. Every tool call is gated against the
`.albedo.yaml` manifest's `repo.github.{owner,repo}`: a mismatch
returns a structured error and performs zero outbound traffic.
Each completed call (success or failure) writes one audit line via
`mcp_audit.record_audit`.

Protocol surface is minimal stdio JSON-RPC 2.0: `initialize`,
`tools/list`, `tools/call`, `notifications/initialized`, and `ping`.
That's enough for Claude Code's MCP client; we deliberately do not
expose anything else.

Tool inventory cross-references current `mcp__github__` call sites in
the repo (worker prompt templates, comment_redispatch / ci_redispatch
flows). The PR description ships the canonical list with the
underlying GitHub API endpoint for each, so reviewers can confirm
nothing was accidentally narrowed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, cast

import httpx

from albedo import paths
from albedo.mcp_audit import record_audit, summarize_args
from albedo.repo_config import RepoManifest, load_repo_manifest

GITHUB_PAT_ENV = 'GITHUB_PERSONAL_ACCESS_TOKEN'
DEFAULT_BASE_URL = 'https://api.github.com'
PROTOCOL_VERSION = '2024-11-05'
SERVER_NAME = 'albedo-github-proxy'
SERVER_VERSION = '0.1.0'
DEFAULT_TIMEOUT_SECONDS = 30.0

# JSON-RPC 2.0 standard error codes.
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One MCP tool exposed by this proxy.

    `build_request` maps validated tool args to an httpx-shaped tuple
    `(method, path, params, json_body)`. The owner/repo gate runs
    before this — handlers receive args known to match the manifest.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    build_request: Callable[
        [dict[str, Any]],
        tuple[str, str, dict[str, Any] | None, Any | None],
    ]


def _build_create_pull_request(
    args: dict[str, Any],
) -> tuple[str, str, dict[str, Any] | None, Any | None]:
    body = {
        'title': args['title'],
        'head': args['head'],
        'base': args['base'],
    }
    if 'body' in args:
        body['body'] = args['body']
    if 'draft' in args:
        body['draft'] = args['draft']
    return 'POST', f'/repos/{args["owner"]}/{args["repo"]}/pulls', None, body


def _build_get_pull_request(
    args: dict[str, Any],
) -> tuple[str, str, dict[str, Any] | None, Any | None]:
    return (
        'GET',
        f'/repos/{args["owner"]}/{args["repo"]}/pulls/{args["pull_number"]}',
        None,
        None,
    )


def _build_list_pull_requests(
    args: dict[str, Any],
) -> tuple[str, str, dict[str, Any] | None, Any | None]:
    params: dict[str, Any] = {'per_page': args.get('per_page', 100)}
    for key in ('state', 'head', 'base', 'sort', 'direction'):
        if key in args:
            params[key] = args[key]
    return 'GET', f'/repos/{args["owner"]}/{args["repo"]}/pulls', params, None


def _build_get_pull_request_files(
    args: dict[str, Any],
) -> tuple[str, str, dict[str, Any] | None, Any | None]:
    return (
        'GET',
        f'/repos/{args["owner"]}/{args["repo"]}/pulls/{args["pull_number"]}/files',
        None,
        None,
    )


def _build_get_pull_request_reviews(
    args: dict[str, Any],
) -> tuple[str, str, dict[str, Any] | None, Any | None]:
    return (
        'GET',
        f'/repos/{args["owner"]}/{args["repo"]}/pulls/{args["pull_number"]}/reviews',
        None,
        None,
    )


def _build_get_pull_request_comments(
    args: dict[str, Any],
) -> tuple[str, str, dict[str, Any] | None, Any | None]:
    return (
        'GET',
        f'/repos/{args["owner"]}/{args["repo"]}/pulls/{args["pull_number"]}/comments',
        None,
        None,
    )


def _build_get_pull_request_status(
    args: dict[str, Any],
) -> tuple[str, str, dict[str, Any] | None, Any | None]:
    return (
        'GET',
        f'/repos/{args["owner"]}/{args["repo"]}/commits/{args["ref"]}/status',
        None,
        None,
    )


def _build_create_pull_request_review(
    args: dict[str, Any],
) -> tuple[str, str, dict[str, Any] | None, Any | None]:
    body: dict[str, Any] = {'event': args.get('event', 'COMMENT')}
    for key in ('body', 'commit_id', 'comments'):
        if key in args:
            body[key] = args[key]
    return (
        'POST',
        f'/repos/{args["owner"]}/{args["repo"]}/pulls/{args["pull_number"]}/reviews',
        None,
        body,
    )


def _build_add_issue_comment(
    args: dict[str, Any],
) -> tuple[str, str, dict[str, Any] | None, Any | None]:
    return (
        'POST',
        f'/repos/{args["owner"]}/{args["repo"]}/issues/{args["issue_number"]}/comments',
        None,
        {'body': args['body']},
    )


def _build_list_workflow_runs(
    args: dict[str, Any],
) -> tuple[str, str, dict[str, Any] | None, Any | None]:
    params: dict[str, Any] = {'per_page': args.get('per_page', 100)}
    for key in ('branch', 'event', 'status', 'actor', 'head_sha'):
        if key in args:
            params[key] = args[key]
    return (
        'GET',
        f'/repos/{args["owner"]}/{args["repo"]}/actions/runs',
        params,
        None,
    )


def _build_list_workflow_jobs(
    args: dict[str, Any],
) -> tuple[str, str, dict[str, Any] | None, Any | None]:
    return (
        'GET',
        f'/repos/{args["owner"]}/{args["repo"]}/actions/runs/{args["run_id"]}/jobs',
        None,
        None,
    )


def _build_get_job_logs(
    args: dict[str, Any],
) -> tuple[str, str, dict[str, Any] | None, Any | None]:
    return (
        'GET',
        f'/repos/{args["owner"]}/{args["repo"]}/actions/jobs/{args["job_id"]}/logs',
        None,
        None,
    )


def _build_rerun_workflow_run(
    args: dict[str, Any],
) -> tuple[str, str, dict[str, Any] | None, Any | None]:
    return (
        'POST',
        f'/repos/{args["owner"]}/{args["repo"]}/actions/runs/{args["run_id"]}/rerun',
        None,
        None,
    )


def _owner_repo_required() -> dict[str, Any]:
    return {
        'owner': {'type': 'string'},
        'repo': {'type': 'string'},
    }


TOOL_REGISTRY: tuple[ToolSpec, ...] = (
    ToolSpec(
        name='create_pull_request',
        description='Open a pull request. POST /repos/{owner}/{repo}/pulls.',
        input_schema={
            'type': 'object',
            'properties': {
                **_owner_repo_required(),
                'title': {'type': 'string'},
                'head': {'type': 'string'},
                'base': {'type': 'string'},
                'body': {'type': 'string'},
                'draft': {'type': 'boolean'},
            },
            'required': ['owner', 'repo', 'title', 'head', 'base'],
        },
        build_request=_build_create_pull_request,
    ),
    ToolSpec(
        name='get_pull_request',
        description=(
            'Read a pull request. GET /repos/{owner}/{repo}/pulls/{pull_number}.'
        ),
        input_schema={
            'type': 'object',
            'properties': {
                **_owner_repo_required(),
                'pull_number': {'type': 'integer'},
            },
            'required': ['owner', 'repo', 'pull_number'],
        },
        build_request=_build_get_pull_request,
    ),
    ToolSpec(
        name='list_pull_requests',
        description='List pull requests. GET /repos/{owner}/{repo}/pulls.',
        input_schema={
            'type': 'object',
            'properties': {
                **_owner_repo_required(),
                'state': {'type': 'string'},
                'head': {'type': 'string'},
                'base': {'type': 'string'},
                'sort': {'type': 'string'},
                'direction': {'type': 'string'},
                'per_page': {'type': 'integer'},
            },
            'required': ['owner', 'repo'],
        },
        build_request=_build_list_pull_requests,
    ),
    ToolSpec(
        name='get_pull_request_files',
        description=(
            'Read PR diff (per-file changes). '
            'GET /repos/{owner}/{repo}/pulls/{pull_number}/files.'
        ),
        input_schema={
            'type': 'object',
            'properties': {
                **_owner_repo_required(),
                'pull_number': {'type': 'integer'},
            },
            'required': ['owner', 'repo', 'pull_number'],
        },
        build_request=_build_get_pull_request_files,
    ),
    ToolSpec(
        name='get_pull_request_reviews',
        description=(
            'List PR reviews. GET /repos/{owner}/{repo}/pulls/{pull_number}/reviews.'
        ),
        input_schema={
            'type': 'object',
            'properties': {
                **_owner_repo_required(),
                'pull_number': {'type': 'integer'},
            },
            'required': ['owner', 'repo', 'pull_number'],
        },
        build_request=_build_get_pull_request_reviews,
    ),
    ToolSpec(
        name='get_pull_request_comments',
        description=(
            'List line-anchored review comments on a PR. '
            'GET /repos/{owner}/{repo}/pulls/{pull_number}/comments.'
        ),
        input_schema={
            'type': 'object',
            'properties': {
                **_owner_repo_required(),
                'pull_number': {'type': 'integer'},
            },
            'required': ['owner', 'repo', 'pull_number'],
        },
        build_request=_build_get_pull_request_comments,
    ),
    ToolSpec(
        name='get_pull_request_status',
        description=(
            'Read combined CI status for a commit ref. '
            'GET /repos/{owner}/{repo}/commits/{ref}/status.'
        ),
        input_schema={
            'type': 'object',
            'properties': {
                **_owner_repo_required(),
                'ref': {'type': 'string'},
            },
            'required': ['owner', 'repo', 'ref'],
        },
        build_request=_build_get_pull_request_status,
    ),
    ToolSpec(
        name='create_pull_request_review',
        description=(
            'Post a PR review (event=COMMENT/APPROVE/REQUEST_CHANGES). '
            'POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews.'
        ),
        input_schema={
            'type': 'object',
            'properties': {
                **_owner_repo_required(),
                'pull_number': {'type': 'integer'},
                'body': {'type': 'string'},
                'event': {'type': 'string'},
                'commit_id': {'type': 'string'},
                'comments': {'type': 'array'},
            },
            'required': ['owner', 'repo', 'pull_number'],
        },
        build_request=_build_create_pull_request_review,
    ),
    ToolSpec(
        name='add_issue_comment',
        description=(
            'Comment on a PR or issue (PRs are issues for the comment endpoint). '
            'POST /repos/{owner}/{repo}/issues/{issue_number}/comments.'
        ),
        input_schema={
            'type': 'object',
            'properties': {
                **_owner_repo_required(),
                'issue_number': {'type': 'integer'},
                'body': {'type': 'string'},
            },
            'required': ['owner', 'repo', 'issue_number', 'body'],
        },
        build_request=_build_add_issue_comment,
    ),
    ToolSpec(
        name='list_workflow_runs',
        description='List workflow runs. GET /repos/{owner}/{repo}/actions/runs.',
        input_schema={
            'type': 'object',
            'properties': {
                **_owner_repo_required(),
                'branch': {'type': 'string'},
                'event': {'type': 'string'},
                'status': {'type': 'string'},
                'actor': {'type': 'string'},
                'head_sha': {'type': 'string'},
                'per_page': {'type': 'integer'},
            },
            'required': ['owner', 'repo'],
        },
        build_request=_build_list_workflow_runs,
    ),
    ToolSpec(
        name='list_workflow_jobs',
        description=(
            'List jobs of a workflow run. '
            'GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs.'
        ),
        input_schema={
            'type': 'object',
            'properties': {
                **_owner_repo_required(),
                'run_id': {'type': 'integer'},
            },
            'required': ['owner', 'repo', 'run_id'],
        },
        build_request=_build_list_workflow_jobs,
    ),
    ToolSpec(
        name='get_job_logs',
        description=(
            'Fetch raw logs for a workflow job. '
            'GET /repos/{owner}/{repo}/actions/jobs/{job_id}/logs.'
        ),
        input_schema={
            'type': 'object',
            'properties': {
                **_owner_repo_required(),
                'job_id': {'type': 'integer'},
            },
            'required': ['owner', 'repo', 'job_id'],
        },
        build_request=_build_get_job_logs,
    ),
    ToolSpec(
        name='rerun_workflow_run',
        description=(
            'Redispatch (rerun) a workflow run. '
            'POST /repos/{owner}/{repo}/actions/runs/{run_id}/rerun.'
        ),
        input_schema={
            'type': 'object',
            'properties': {
                **_owner_repo_required(),
                'run_id': {'type': 'integer'},
            },
            'required': ['owner', 'repo', 'run_id'],
        },
        build_request=_build_rerun_workflow_run,
    ),
)

_TOOLS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOL_REGISTRY}


class PatNotFoundError(RuntimeError):
    """Raised when `$ALBEDO_HOME/.env` does not yield a PAT at startup."""


def _parse_env_file(text: str) -> dict[str, str]:
    """Parse a minimal `KEY=value` `.env` file. Mirrors `git_credential`."""
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        name, _, value = line.partition('=')
        result[name.strip()] = value.strip().strip('"').strip("'")
    return result


def load_pat_from_env_file(env_path: Path) -> str:
    """Read the PAT directly from `env_path`, ignoring process env.

    The proxy must NEVER read `GITHUB_PERSONAL_ACCESS_TOKEN` from its
    own process env — `.env` is the single source of truth so the
    spawning claude cannot accidentally leak its environment-injected
    PAT into this process.

    Raises `PatNotFoundError` with an actionable message when the file
    is missing, unreadable, or does not define a non-empty PAT entry.
    """
    if not env_path.exists():
        raise PatNotFoundError(f'{env_path} not found; cannot resolve {GITHUB_PAT_ENV}')
    try:
        text = env_path.read_text(encoding='utf-8')
    except OSError as exc:
        raise PatNotFoundError(f'failed to read {env_path}: {exc}') from exc
    overlay = _parse_env_file(text)
    token = overlay.get(GITHUB_PAT_ENV, '').strip()
    if not token:
        raise PatNotFoundError(f'{GITHUB_PAT_ENV} missing or empty in {env_path}')
    return token


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    """Resolved runtime config for a single proxy process."""

    pat: str
    allowed_owner: str
    allowed_repo: str
    state_dir: Path
    issue_id: str
    agent_id: str
    base_url: str = DEFAULT_BASE_URL


def _extract_github_coords(manifest: RepoManifest) -> tuple[str, str]:
    if manifest.repo.github is None:
        raise RuntimeError(
            'repo.github.{owner,repo} missing in .albedo.yaml — '
            'the proxy cannot enforce a repo gate without it.'
        )
    return manifest.repo.github.owner, manifest.repo.github.repo


def _make_error(
    request_id: Any, code: int, message: str, *, data: Any = None
) -> dict[str, Any]:
    error: dict[str, Any] = {'code': code, 'message': message}
    if data is not None:
        error['data'] = data
    return {'jsonrpc': '2.0', 'id': request_id, 'error': error}


def _make_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def _tool_error_payload(message: str) -> dict[str, Any]:
    """Build an MCP `tools/call` error envelope.

    MCP's spec says tool-side errors come back as a normal result with
    `isError: true` and a text content block, not as a JSON-RPC error.
    The caller LLM can read the message and recover.
    """
    return {
        'isError': True,
        'content': [{'type': 'text', 'text': message}],
    }


def _tool_text_result(text: str) -> dict[str, Any]:
    return {'isError': False, 'content': [{'type': 'text', 'text': text}]}


def _check_owner_repo(
    args: dict[str, Any], allowed_owner: str, allowed_repo: str
) -> str | None:
    """Validate `args` against the manifest. Return an error message or None."""
    owner = args.get('owner')
    repo = args.get('repo')
    if not isinstance(owner, str) or not isinstance(repo, str):
        return (
            'tool args missing owner/repo — proxy refuses calls without '
            'an explicit, gate-checkable target repo'
        )
    if owner != allowed_owner or repo != allowed_repo:
        return (
            f'repo gate refused: tool targeted {owner}/{repo}, but '
            f'.albedo.yaml only authorises {allowed_owner}/{allowed_repo}'
        )
    return None


def _execute_tool(
    *,
    spec: ToolSpec,
    args: dict[str, Any],
    config: ProxyConfig,
    client: httpx.Client,
) -> tuple[str, dict[str, Any]]:
    """Issue the upstream HTTP call and shape an MCP tool result.

    Returns `(outcome, mcp_result_envelope)`. `outcome` is `'ok'` for
    a 2xx response and `'error'` for anything else (including
    transport failures); both are still returned to the caller as a
    structured tool result so the LLM can see the GitHub error body.
    """
    method, path, params, json_body = spec.build_request(args)
    try:
        response = client.request(
            method,
            path,
            params=params,
            json=json_body,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return 'error', _tool_error_payload(f'github transport error: {exc}')
    body_text = response.text
    if response.status_code >= 400:
        return (
            'error',
            _tool_error_payload(f'github HTTP {response.status_code}: {body_text}'),
        )
    return 'ok', _tool_text_result(body_text)


def _build_tools_list_result() -> dict[str, Any]:
    return {
        'tools': [
            {
                'name': spec.name,
                'description': spec.description,
                'inputSchema': spec.input_schema,
            }
            for spec in TOOL_REGISTRY
        ]
    }


def _build_initialize_result() -> dict[str, Any]:
    return {
        'protocolVersion': PROTOCOL_VERSION,
        'capabilities': {'tools': {}},
        'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
    }


def handle_tool_call(
    *,
    request_id: Any,
    params: dict[str, Any],
    config: ProxyConfig,
    client: httpx.Client,
) -> dict[str, Any]:
    """Dispatch one `tools/call` request, including audit + repo gate.

    Always emits one audit line per call. The repo gate runs before
    any HTTP request so a mismatch produces zero outbound traffic.
    """
    tool_name = params.get('name')
    raw_args: Any = params.get('arguments') or {}
    if not isinstance(tool_name, str):
        return _make_error(
            request_id, JSONRPC_INVALID_PARAMS, 'tools/call missing string `name`'
        )
    if not isinstance(raw_args, dict):
        return _make_error(
            request_id,
            JSONRPC_INVALID_PARAMS,
            'tools/call `arguments` must be an object',
        )
    args: dict[str, Any] = cast('dict[str, Any]', raw_args)
    spec = _TOOLS_BY_NAME.get(tool_name)
    if spec is None:
        record_audit(
            state_dir=config.state_dir,
            issue_id=config.issue_id,
            agent_id=config.agent_id,
            tool=f'github.{tool_name}',
            args_summary=summarize_args(args),
            outcome='refused',
            latency_ms=0,
        )
        return _make_error(
            request_id,
            JSONRPC_METHOD_NOT_FOUND,
            f'unknown tool: {tool_name}',
        )

    started = time.monotonic()
    gate_error = _check_owner_repo(args, config.allowed_owner, config.allowed_repo)
    if gate_error is not None:
        latency_ms = int((time.monotonic() - started) * 1000)
        record_audit(
            state_dir=config.state_dir,
            issue_id=config.issue_id,
            agent_id=config.agent_id,
            tool=f'github.{tool_name}',
            args_summary=summarize_args(args),
            outcome='refused',
            latency_ms=latency_ms,
        )
        return _make_result(request_id, _tool_error_payload(gate_error))

    outcome, envelope = _execute_tool(
        spec=spec, args=args, config=config, client=client
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    record_audit(
        state_dir=config.state_dir,
        issue_id=config.issue_id,
        agent_id=config.agent_id,
        tool=f'github.{tool_name}',
        args_summary=summarize_args(args),
        outcome=outcome,
        latency_ms=latency_ms,
    )
    return _make_result(request_id, envelope)


def handle_message(
    *,
    message: dict[str, Any],
    config: ProxyConfig,
    client: httpx.Client,
) -> dict[str, Any] | None:
    """Route one parsed JSON-RPC message; return a response or None.

    None means the message was a notification (no response expected
    per JSON-RPC 2.0).
    """
    method = message.get('method')
    request_id = message.get('id')
    params_obj: Any = message.get('params')
    params: dict[str, Any] = (
        cast('dict[str, Any]', params_obj) if isinstance(params_obj, dict) else {}
    )

    if not isinstance(method, str):
        if request_id is None:
            return None
        return _make_error(
            request_id, JSONRPC_INVALID_REQUEST, 'missing string `method`'
        )

    if method.startswith('notifications/') or request_id is None:
        return None

    if method == 'initialize':
        return _make_result(request_id, _build_initialize_result())
    if method == 'ping':
        return _make_result(request_id, {})
    if method == 'tools/list':
        return _make_result(request_id, _build_tools_list_result())
    if method == 'tools/call':
        return handle_tool_call(
            request_id=request_id,
            params=params,
            config=config,
            client=client,
        )
    return _make_error(
        request_id, JSONRPC_METHOD_NOT_FOUND, f'unknown method: {method}'
    )


def _make_client(
    pat: str,
    *,
    base_url: str,
    transport: httpx.BaseTransport | None,
) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        transport=transport,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        # GitHub's job-logs endpoint replies 302 → S3; without
        # follow_redirects the proxy would surface an empty body as ok.
        follow_redirects=True,
        headers={
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {pat}',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': f'{SERVER_NAME}/{SERVER_VERSION}',
        },
    )


def serve(
    *,
    config: ProxyConfig,
    stdin: Iterable[str],
    stdout: IO[str],
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Run the stdio JSON-RPC loop until stdin closes.

    One JSON object per line on both directions — Claude Code's MCP
    client speaks line-delimited JSON-RPC over stdio, so we don't
    need framed Content-Length headers here.
    """
    with _make_client(
        config.pat, base_url=config.base_url, transport=transport
    ) as client:
        for raw_line in stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                message: Any = json.loads(line)
            except json.JSONDecodeError as exc:
                _write_message(
                    stdout,
                    _make_error(
                        None,
                        JSONRPC_PARSE_ERROR,
                        f'failed to parse JSON-RPC message: {exc}',
                    ),
                )
                continue
            if not isinstance(message, dict):
                _write_message(
                    stdout,
                    _make_error(
                        None,
                        JSONRPC_INVALID_REQUEST,
                        'JSON-RPC message must be an object',
                    ),
                )
                continue
            response = handle_message(
                message=cast('dict[str, Any]', message),
                config=config,
                client=client,
            )
            if response is not None:
                _write_message(stdout, response)


def _write_message(stdout: IO[str], message: dict[str, Any]) -> None:
    stdout.write(json.dumps(message, ensure_ascii=False) + '\n')
    stdout.flush()


def _strip_pat_from_environ() -> None:
    """Defensive: drop any inherited `GITHUB_PERSONAL_ACCESS_TOKEN`.

    The proxy must never read the PAT from its own process env. We
    pop it on startup so that any later code path which accidentally
    consults `os.environ` (httpx auth helpers, transitive deps) cannot
    silently fall back to a leaked token.
    """
    os.environ.pop(GITHUB_PAT_ENV, None)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='albedo-mcp-github-proxy',
        description=(
            'Stdio MCP proxy for the narrow GitHub surface Albedo uses. '
            'Loads the PAT from $ALBEDO_HOME/.env and gates calls to the '
            'repo declared in .albedo.yaml.'
        ),
    )
    parser.add_argument(
        '--repo-root',
        type=Path,
        required=True,
        help='Repo root containing .albedo.yaml (used for the owner/repo gate).',
    )
    parser.add_argument(
        '--state-dir',
        type=Path,
        required=True,
        help='Per-project state dir (audit log lands under <state>/logs/).',
    )
    parser.add_argument(
        '--issue-id',
        required=True,
        help='Linear issue identifier for audit attribution.',
    )
    parser.add_argument(
        '--agent-id',
        required=True,
        help='Per-process agent slot identifier for audit attribution.',
    )
    return parser


def resolve_config(args: argparse.Namespace) -> ProxyConfig:
    """Load PAT + manifest into a `ProxyConfig`. Raises on failure."""
    pat = load_pat_from_env_file(paths.env_file_path())
    _, manifest = load_repo_manifest(Path(args.repo_root))
    owner, repo = _extract_github_coords(manifest)
    return ProxyConfig(
        pat=pat,
        allowed_owner=owner,
        allowed_repo=repo,
        state_dir=Path(args.state_dir),
        issue_id=str(args.issue_id),
        agent_id=str(args.agent_id),
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Returns process exit code; never raises on bad PAT/manifest."""
    _strip_pat_from_environ()
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        config = resolve_config(args)
    except PatNotFoundError as exc:
        sys.stderr.write(f'mcp-github-proxy: {exc}\n')
        return 2
    except (RuntimeError, OSError, ValueError) as exc:
        sys.stderr.write(f'mcp-github-proxy: failed to load config: {exc}\n')
        return 3
    serve(config=config, stdin=sys.stdin, stdout=sys.stdout)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
