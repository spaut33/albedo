"""Stdio MCP proxy that exposes a constrained Linear surface.

The proxy is launched by a worker as a child process and listed in the
spawned claude's `mcp-servers.json`. It holds the `LINEAR_API_KEY`
in-process — resolved via `config.load_linear_api_key` with the
existing per-agent override pattern — so the secret never travels
through claude env substitution.

The agent issue is passed in via CLI flags (`--issue-identifier`,
`--issue-uuid`); every tool call's `issue_id` argument is compared
against those locally before any outbound Linear request. Calls
referring to a different issue are refused with a structured error,
which keeps the proxy strictly within `.albedo.yaml`'s `linear.project`
scope by construction. Each call (success, refusal, or transport
error) writes one line to `<state_dir>/logs/mcp-audit.jsonl` via
`mcp_audit.record_audit`.

Tool inventory:
  * `linear_get_issue`  — read the bound issue (body, state, metadata)
  * `linear_list_comments` — read prior reviewer / user feedback
  * `linear_add_comment` — post a comment
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, cast

from pydantic import SecretStr

from albedo.config import LINEAR_API_KEY_ENV, load_linear_api_key
from albedo.linear_client import LinearClient, LinearError
from albedo.mcp_audit import record_audit, summarize_args

PROTOCOL_VERSION = '2024-11-05'
SERVER_NAME = 'albedo-linear-proxy'
SERVER_VERSION = '0.1.0'

OUTCOME_OK = 'ok'
OUTCOME_SCOPE_DENIED = 'scope_denied'
OUTCOME_BAD_ARGS = 'bad_args'
OUTCOME_ERROR = 'error'

# JSON-RPC 2.0 error codes we reuse.
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


class _BadArgsError(Exception):
    """Raised by tool implementations on argument validation failure.

    Caught in `_dispatch` so the audit outcome reflects bad-args, not ok.
    """


TOOLS: tuple[dict[str, Any], ...] = (
    {
        'name': 'linear_get_issue',
        'description': (
            'Fetch the bound Linear issue by UUID or shorthand identifier '
            '(e.g. "AI-73"). Returns title, body, state, URL, and labels.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'issue_id': {
                    'type': 'string',
                    'description': 'Linear UUID or shorthand identifier.',
                },
            },
            'required': ['issue_id'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'linear_list_comments',
        'description': (
            'List comments on the bound Linear issue, oldest first. '
            'Use to read prior reviewer / user feedback before replying.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'issue_id': {
                    'type': 'string',
                    'description': 'Linear UUID or shorthand identifier.',
                },
            },
            'required': ['issue_id'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'linear_add_comment',
        'description': (
            'Post a comment on the bound Linear issue. Calls targeting any '
            'other issue are refused without contacting Linear.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'issue_id': {
                    'type': 'string',
                    'description': 'Linear UUID or shorthand identifier.',
                },
                'body': {
                    'type': 'string',
                    'description': 'Markdown comment body.',
                },
            },
            'required': ['issue_id', 'body'],
            'additionalProperties': False,
        },
    },
)


@dataclass(frozen=True, slots=True)
class ProxyContext:
    """Static configuration injected by the launching worker.

    `issue_identifier` is the Linear shorthand (e.g. "AI-73"); `issue_uuid`
    is the corresponding Linear UUID. Either may match a tool call's
    `issue_id` argument. `project_id` is recorded in audit context and
    surfaced in scope-denied error payloads.
    """

    issue_identifier: str
    issue_uuid: str
    agent_id: str
    project_id: str
    state_dir: Path

    @property
    def allowed_ids(self) -> frozenset[str]:
        return frozenset({self.issue_identifier, self.issue_uuid})


class LinearProxyHandler:
    """Tool dispatcher backed by `LinearClient` plus scope/audit guards.

    Split out from the stdio loop so unit tests can drive the handler
    directly without spawning a subprocess.
    """

    def __init__(
        self,
        client: LinearClient,
        context: ProxyContext,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._context = context
        self._clock = clock

    def list_tools(self) -> list[dict[str, Any]]:
        return [dict(t) for t in TOOLS]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a `tools/call` invocation and audit the outcome."""
        if name == 'linear_get_issue':
            return self._dispatch(name, arguments, self._do_get_issue)
        if name == 'linear_list_comments':
            return self._dispatch(name, arguments, self._do_list_comments)
        if name == 'linear_add_comment':
            return self._dispatch(name, arguments, self._do_add_comment)
        started = self._clock()
        self._audit(name, arguments, OUTCOME_BAD_ARGS, started)
        return _error_result(f'Unknown tool {name!r}')

    def _dispatch(
        self,
        name: str,
        arguments: dict[str, Any],
        impl: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        started = self._clock()
        issue_id = arguments.get('issue_id')
        if not isinstance(issue_id, str) or not issue_id.strip():
            outcome = OUTCOME_BAD_ARGS
            payload = _error_result("Missing or empty 'issue_id' argument.")
        elif issue_id not in self._context.allowed_ids:
            outcome = OUTCOME_SCOPE_DENIED
            payload = _scope_denied_result(issue_id, self._context)
        else:
            try:
                payload = impl(arguments)
                outcome = OUTCOME_OK
            except _BadArgsError as exc:
                outcome = OUTCOME_BAD_ARGS
                payload = _error_result(str(exc))
            except LinearError as exc:
                outcome = OUTCOME_ERROR
                payload = _error_result(f'Linear API error: {exc}')
        self._audit(name, arguments, outcome, started)
        return payload

    def _do_get_issue(self, arguments: dict[str, Any]) -> dict[str, Any]:
        issue = self._client.get_issue(arguments['issue_id'])
        return _ok_result(_issue_to_payload(issue))

    def _do_list_comments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        comments = self._client.list_comments(arguments['issue_id'])
        return _ok_result([dataclasses.asdict(c) for c in comments])

    def _do_add_comment(self, arguments: dict[str, Any]) -> dict[str, Any]:
        body = arguments.get('body')
        if not isinstance(body, str) or not body.strip():
            raise _BadArgsError("Missing or empty 'body' argument.")
        comment_id = self._client.add_comment(arguments['issue_id'], body)
        return _ok_result({'comment_id': comment_id})

    def _audit(
        self,
        tool: str,
        arguments: dict[str, Any],
        outcome: str,
        started: float,
    ) -> None:
        latency_ms = max(0, int((self._clock() - started) * 1000))
        record_audit(
            state_dir=self._context.state_dir,
            issue_id=self._context.issue_identifier,
            agent_id=self._context.agent_id,
            tool=tool,
            args_summary=summarize_args(_redact_args(tool, arguments)),
            outcome=outcome,
            latency_ms=latency_ms,
        )


def _redact_args(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Trim large free-text fields before they hit the audit log.

    `mcp_audit.summarize_args` already truncates to 256 chars, but a verbose
    comment body would still dominate the line. Keep just a length hint.
    """
    if tool == 'linear_add_comment' and 'body' in arguments:
        body = arguments['body']
        body_repr = f'<{len(body)} chars>' if isinstance(body, str) else '<non-str>'
        return {**arguments, 'body': body_repr}
    return dict(arguments)


def _issue_to_payload(issue: Any) -> dict[str, Any]:
    return {
        'id': issue.id,
        'identifier': issue.identifier,
        'title': issue.title,
        'description': issue.description,
        'url': issue.url,
        'state_id': issue.state_id,
        'state_name': issue.state_name,
        'assignee_id': issue.assignee_id,
        'parent_id': issue.parent_id,
        'branch_name': issue.branch_name,
        'label_ids': list(issue.label_ids),
        'label_names': list(issue.label_names),
    }


def _ok_result(payload: Any) -> dict[str, Any]:
    return {
        'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False)}],
        'isError': False,
    }


def _error_result(
    message: str, *, structured: dict[str, Any] | None = None
) -> dict[str, Any]:
    text = json.dumps(
        structured if structured is not None else {'error': message},
        ensure_ascii=False,
    )
    return {
        'content': [{'type': 'text', 'text': text}],
        'isError': True,
    }


def _scope_denied_result(issue_id: str, ctx: ProxyContext) -> dict[str, Any]:
    return _error_result(
        '',
        structured={
            'error': 'scope_denied',
            'message': (
                f'Tool call targeting {issue_id!r} refused: only the bound '
                f'issue {ctx.issue_identifier!r} (within Linear project '
                f'{ctx.project_id!r}) is in scope for this proxy.'
            ),
            'requested_issue_id': issue_id,
            'allowed_issue_identifier': ctx.issue_identifier,
            'project_id': ctx.project_id,
        },
    )


# --- stdio JSON-RPC plumbing -------------------------------------------------


def _jsonrpc_response(req_id: Any, result: Any) -> dict[str, Any]:
    return {'jsonrpc': '2.0', 'id': req_id, 'result': result}


def _jsonrpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        'jsonrpc': '2.0',
        'id': req_id,
        'error': {'code': code, 'message': message},
    }


def handle_message(
    handler: LinearProxyHandler,
    message: dict[str, Any],
) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC message; return a response dict or None.

    Notifications (messages without `id`) get no response.
    """
    method = message.get('method')
    req_id = message.get('id')
    is_notification = req_id is None

    if not isinstance(method, str):
        if is_notification:
            return None
        return _jsonrpc_error(req_id, JSONRPC_INVALID_REQUEST, 'Missing method')

    if method == 'initialize':
        return _jsonrpc_response(
            req_id,
            {
                'protocolVersion': PROTOCOL_VERSION,
                'capabilities': {'tools': {}},
                'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
            },
        )

    if method.startswith('notifications/'):
        return None

    if method == 'tools/list':
        return _jsonrpc_response(req_id, {'tools': handler.list_tools()})

    if method == 'tools/call':
        params_raw = message.get('params')
        if not isinstance(params_raw, dict):
            return _jsonrpc_error(
                req_id, JSONRPC_INVALID_PARAMS, 'Invalid tools/call params'
            )
        params = cast('dict[str, Any]', params_raw)
        tool_name = params.get('name')
        arguments_raw = params.get('arguments')
        if not isinstance(tool_name, str) or not isinstance(arguments_raw, dict):
            return _jsonrpc_error(
                req_id, JSONRPC_INVALID_PARAMS, 'Invalid tools/call params'
            )
        arguments = cast('dict[str, Any]', arguments_raw)
        result = handler.call_tool(tool_name, arguments)
        return _jsonrpc_response(req_id, result)

    if is_notification:
        return None
    return _jsonrpc_error(
        req_id, JSONRPC_METHOD_NOT_FOUND, f'Unknown method {method!r}'
    )


def serve_stdio(
    handler: LinearProxyHandler,
    *,
    input_stream: IO[str] | None = None,
    output_stream: IO[str] | None = None,
) -> None:
    """Read newline-delimited JSON-RPC requests; write responses.

    Loops until stdin closes. Malformed lines emit a parse-error response
    (with `id=null`) but do not abort the loop.
    """
    inp = input_stream if input_stream is not None else sys.stdin
    out = output_stream if output_stream is not None else sys.stdout

    for raw_line in inp:
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed: Any = json.loads(line)
        except json.JSONDecodeError:
            _write_response(
                out, _jsonrpc_error(None, JSONRPC_PARSE_ERROR, 'Invalid JSON')
            )
            continue
        if not isinstance(parsed, dict):
            _write_response(
                out,
                _jsonrpc_error(None, JSONRPC_INVALID_REQUEST, 'Expected object'),
            )
            continue
        message = cast('dict[str, Any]', parsed)
        response = handle_message(handler, message)
        if response is not None:
            _write_response(out, response)


def _write_response(stream: IO[str], response: dict[str, Any]) -> None:
    stream.write(json.dumps(response, ensure_ascii=False) + '\n')
    stream.flush()


# --- CLI entry point ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='albedo-mcp-linear-proxy',
        description=(
            'Stdio MCP proxy exposing a project-scoped Linear surface to a '
            'spawned claude. Holds LINEAR_API_KEY in-process; refuses calls '
            "outside the bound issue's scope."
        ),
    )
    parser.add_argument(
        '--issue-identifier',
        required=True,
        help='Linear shorthand identifier of the bound issue (e.g. "AI-73").',
    )
    parser.add_argument(
        '--issue-uuid',
        required=True,
        help='Linear UUID of the bound issue.',
    )
    parser.add_argument(
        '--agent-id',
        required=True,
        help='Worker/agent id used for per-agent token override and audit.',
    )
    parser.add_argument(
        '--project-id',
        required=True,
        help='Linear project UUID; surfaced in scope-denied errors.',
    )
    parser.add_argument(
        '--state-dir',
        required=True,
        type=Path,
        help='State directory; audit log lands at <state-dir>/logs/mcp-audit.jsonl.',
    )
    parser.add_argument(
        '--api-url',
        default='https://api.linear.app/graphql',
        help='Linear GraphQL endpoint (default: %(default)s).',
    )
    return parser


def _resolve_api_key(agent_id: str) -> SecretStr:
    return load_linear_api_key(agent_id=agent_id)


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        api_key = _resolve_api_key(args.agent_id)
    except RuntimeError as exc:
        print(
            f'albedo-mcp-linear-proxy: {LINEAR_API_KEY_ENV} not configured — {exc}',
            file=sys.stderr,
        )
        return 2

    context = ProxyContext(
        issue_identifier=args.issue_identifier,
        issue_uuid=args.issue_uuid,
        agent_id=args.agent_id,
        project_id=args.project_id,
        state_dir=args.state_dir,
    )
    with LinearClient(args.api_url, api_key) as client:
        handler = LinearProxyHandler(client, context)
        serve_stdio(handler)
    return 0


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
