"""Git credential helper that resolves the GitHub PAT from `$ALBEDO_HOME/.env`.

Invoked by git when an HTTPS fetch/pull/push needs credentials. The PAT is
*not* embedded in `.git/config` — only the path to this module is — so a
worktree's on-disk state contains no token-shaped material. The helper reads
the token at invocation time, every time.

Configured by `worktree.ensure_worktree` as:

    git config --local credential.https://github.com.helper \\
        '!<sys.executable> -m albedo.git_credential'

For `get`: read `$ALBEDO_HOME/.env`, locate `GITHUB_PERSONAL_ACCESS_TOKEN`,
print git's credential protocol (`username=...\\npassword=...\\n\\n`) on
stdout. For `store` / `erase` / unknown: no-op (exit 0). On missing `.env`
or missing PAT key: exit 1 with a clear stderr message.

Process env always wins over `.env` so an operator who exported a different
PAT in their shell can override what's on disk without editing the file.
"""

from __future__ import annotations

import os
import sys

from albedo import paths

GITHUB_PAT_ENV = 'GITHUB_PERSONAL_ACCESS_TOKEN'


def _parse_env_file(text: str) -> dict[str, str]:
    """Parse a minimal `KEY=value` `.env` file. Mirrors `config._env_file_overlay`."""
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        name, _, value = line.partition('=')
        result[name.strip()] = value.strip().strip('"').strip("'")
    return result


def _resolve_pat() -> str | None:
    """Return the PAT, or None when no source supplies one.

    Process env first (so the operator can override), then `$ALBEDO_HOME/.env`.
    Returns None for both "file missing" and "key missing"; the caller decides
    which stderr message to emit.
    """
    env_value = os.environ.get(GITHUB_PAT_ENV, '').strip()
    if env_value:
        return env_value
    env_path = paths.env_file_path()
    if not env_path.exists():
        return None
    overlay = _parse_env_file(env_path.read_text(encoding='utf-8'))
    file_value = overlay.get(GITHUB_PAT_ENV, '').strip()
    if file_value:
        return file_value
    return None


def _emit_failure(reason: str) -> int:
    sys.stderr.write(f'albedo git-credential: {reason}\n')
    return 1


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else ''
    if action != 'get':
        # `store` and `erase` are no-ops: this helper is read-only.
        return 0

    # Drain stdin (git sends `host=...\nprotocol=...\n\n`); we don't need it.
    sys.stdin.read()

    env_path = paths.env_file_path()
    has_env_override = bool(os.environ.get(GITHUB_PAT_ENV, '').strip())
    if not has_env_override and not env_path.exists():
        return _emit_failure(
            f'{env_path} not found and {GITHUB_PAT_ENV} not set in process env'
        )

    token = _resolve_pat()
    if token is None:
        return _emit_failure(f'{GITHUB_PAT_ENV} not set in {env_path} or process env')

    sys.stdout.write('username=x-access-token\n')
    sys.stdout.write(f'password={token}\n')
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
