# Albedo

Autonomous engineering on top of Claude Code — from a Linear ticket to
a merge-ready PR, end to end.

N local workers pull tasks from a Linear board, spawn a headless
`claude -p` per task in an isolated git worktree, and open PRs on
GitHub. A human is in the loop only twice: approving the decomposition
and merging the final PR.

Linear is the single source of truth — there is no separate database,
no scheduler, no web UI. Workers are stateless across tasks; any one
can be killed and restarted. Roles are not workers — a worker reads the
column the issue sits in and runs the matching prompt mode.

See [`docs/albedo-concept.md`](docs/albedo-concept.md) for the
architectural concept.

## Status

POC. The end-to-end loop is working: Architect decomposes, Coder opens
PRs, Reviewer comments on GitHub, the supervisor moves issues to Done
on merge, worktrees and old Done issues are GC'd in the background.
This is research-grade software — expect rough edges, audit before
running it against production repos.

## How it works

```
Triage   ──ARCHITECT──►  Awaiting approval  ──human──►  Backlog (children)
                                                            │
                                                          CODER
                                                            ▼
                                                          Review
                                                            │
                                                         REVIEWER
                                                            ▼
                                                   Awaiting approval
                                                            │
                                                       human merge
                                                            ▼
                                                          Done
```

| Role | Trigger | Output |
|---|---|---|
| **Architect** | Issue in `Triage` without a parent | A decomposition proposal — children created in Triage and unblocked once the human approves the parent. The human can edit, drop (`cancelled-by-human` label), or add children before approving — see `docs/albedo-concept.md` §13.1. |
| **Coder** | Issue in `Backlog` (or `Review` after revisions) | A PR pushed to GitHub with `PR: <url>` posted as a Linear comment. |
| **Reviewer** | Issue in `Review` | A GitHub PR review comment plus a `VERDICT:` line; on `APPROVE` the issue moves to `Awaiting approval`, on `REQUEST_CHANGES` it returns to `Backlog`. |

The supervisor handles everything outside the per-task pipeline:
stale-claim recovery, decomposition release, PR-merge → Done sync,
worktree GC, and a daily archive of old Done issues.

## Prerequisites

- Python ≥ 3.12 and [`uv`](https://docs.astral.sh/uv/)
- The [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) on
  `$PATH` (albedo spawns `claude -p` per task)
- Git ≥ 2.20 (worktree support)
- A Linear workspace and a team you own
- A GitHub repository albedo can push branches to and open PRs against

## Quick start

Install once, `cd` into a target repo, run `albedo`. The first invocation
auto-creates the templates it needs and tells you what to edit.

```bash
# 1. Install (one-time)
uv tool install .

# 2. From your target repo
cd ~/code/myrepo
albedo run
# → seeds ~/.config/albedo/{config.yaml,.env,prompts/,mcp-servers.json}
# → exits with: edit linear.team and the secrets, then re-run

$EDITOR ~/.config/albedo/.env          # LINEAR_API_KEY, GITHUB_PERSONAL_ACCESS_TOKEN
$EDITOR ~/.config/albedo/config.yaml   # set linear.team

albedo run
# → seeds .albedo.yaml in the repo (github coords auto-filled from origin)
# → exits with: edit name and linear.project, then re-run

$EDITOR .albedo.yaml                   # set name + linear.project

albedo run                             # actually starts the supervisor + workers
```

First start auto-runs the Linear bootstrap (states + labels) for the
project; subsequent starts skip it (marker at
`~/.local/state/albedo/<name>/.bootstrapped`). `albedo run` walks up
from CWD to find `.albedo.yaml` — same as `git`/`cargo`/`uv`. Stop with
`Ctrl-C` (SIGINT) — workers finish the current `claude -p` spawn before
exiting.

`init`, `init-repo`, `preflight`, and `setup` remain available as
explicit subcommands for re-seeding (with `--force`), debugging, or
running the bootstrap independently — none of them are required for a
fresh setup.

### Single-shot debugging

```bash
cd ~/code/myrepo
albedo --once --issue AI-42 --agent-id 1
```

Runs one CODER iteration against the current worktree state. Useful for
reproducing a bad spawn outside the poll loop. Goes through the same
auto-seed gates as `albedo run`, so first invocation in a fresh
environment will write the templates and ask you to edit them.

## Configuration

`$ALBEDO_HOME/config.yaml` carries cross-repo knobs only:

| Key | Default | Notes |
|---|---|---|
| `workers` | `2` | Worker processes; `--workers N` overrides. Hard cap 16. |
| `poll_interval_seconds` | `20` | Per-worker poll cadence. |
| `state_dir` | (XDG: `~/.local/state/albedo/<name>`) | Override only if you want a custom state location. The `<name>/` suffix is always appended. |
| `worktree_root` | (XDG: `~/.local/share/albedo/worktrees/<name>`) | Same — always per-project scoped. |
| `archive_done_after_days` | `7` | Daily housekeeping archives Done issues older than this. `0` disables. |
| `max_attempts_before_escalation` | `3` | Reviewer `REQUEST_CHANGES` count after which the issue is parked in `Awaiting approval` with the `stuck` label. |
| `linear.team` | (required) | Linear team key (e.g. `ORC`) or exact team name. Shared across all repos. |
| `usage.rolling_window_token_cap` | `200000` | Soft input+output token cap over a rolling window — workers throttle when reached. |
| `models.{coder,reviewer,architect,triage}` | (`null`) | Optional per-role model overrides passed to `claude -p --model`. |

Per-target-repo settings (`name`, `linear.project`, `repo.base_branch`,
`repo.github.{owner,repo}`) live in `.albedo.yaml` at each repo's root.
See [`docs/multi-project.md`](docs/multi-project.md) for the full
manifest schema and multi-repo workflow.

`$ALBEDO_HOME` defaults to `$XDG_CONFIG_HOME/albedo` (i.e.
`~/.config/albedo`). Override per-shell to keep multiple environments
side-by-side: `ALBEDO_HOME=~/configs/albedo-staging albedo run`.

## Secrets

Loaded by pydantic-settings from process env first, then from
`$ALBEDO_HOME/.env` (auto-seeded on the first `albedo run`, or
explicitly by `albedo init`):

- `LINEAR_API_KEY` — required. Per-agent overrides via
  `LINEAR_API_KEY_<AGENT_ID>` (e.g. `LINEAR_API_KEY_1`,
  `LINEAR_API_KEY_2`) so each worker can authenticate as its own Linear
  user.
- `GITHUB_PERSONAL_ACCESS_TOKEN` — required for the GitHub MCP server
  used by Coder and Reviewer. A classic PAT with `repo` scope is enough.

## Logging

Two outputs per process, configured by `albedo.logging_setup`:

- **stderr**: human-readable (`structlog.dev.ConsoleRenderer`) for
  `tail -f` / `journalctl`.
- **`~/.local/state/albedo/<name>/logs/<process>.log`**: JSON, one event
  per line, rotating at 10 MB × 5 backups. Supervisor writes
  `supervisor.log`; each worker writes `agent-<id>.log`.

Bind context (e.g. `issue=AI-42`) once per task and every downstream
log line carries it automatically — see `structlog.contextvars`.

## Project conventions (non-negotiable)

Baked into `pyproject.toml` and enforced from the first commit:

- **Single quotes** everywhere (`ruff format --quote-style=single`).
- **Line length 88**.
- **Lint ruleset**: `E, F, W, I, N, UP, B, A, C4, SIM, RUF`.
- **Test coverage ≥ 80%** — `--cov-fail-under=80` is a hard gate.
- **Type checking**: `pyright` strict on `src/albedo`.

If a change cannot meet these, fix the change, not the rules.

## Layout

```
src/albedo/              # the albedo package
  __main__.py            # CLI: subcommands (init, init-repo, run, setup, preflight)
  paths.py               # XDG resolvers ($ALBEDO_HOME, state_home, worktree_home)
  config.py              # global config schema + secrets
  repo_config.py         # .albedo.yaml schema + walk-up discovery
  init_cmd.py            # `albedo init` and `albedo init-repo`
  supervisor.py          # multi-worker spawn + housekeeping thread
  worker.py              # per-worker loop: poll → claim → role → spawn
  housekeeping.py        # decomposition release, PR-merge sync, GC, archive
  claude_runner.py       # subprocess wrapper around `claude -p`
  linear_client.py       # GraphQL client (no SDK)
  github_client.py       # minimal REST client for PR state
  worktree.py            # git worktree CRUD
  usage.py               # SQLite usage ledger + rate-limit guard
  logging_setup.py       # structlog + per-process JSON file
  setup.py               # idempotent Linear bootstrap
  _data/                 # bundled defaults: prompts/, mcp-servers.json
examples/sample-repo/    # target repo for E2E runs (carries .albedo.yaml)
tests/unit/, tests/integration/

# Runtime locations (auto-created on first `albedo run`):
$ALBEDO_HOME/            # ~/.config/albedo by default
  config.yaml            # global config
  .env                   # secrets
  prompts/               # operator-editable role templates
  mcp-servers.json       # passed to `claude -p --mcp-config`
~/.local/state/albedo/<name>/        # per-project: heartbeats, usage.db, logs/
~/.local/share/albedo/worktrees/<name>/  # per-project worktrees
```

## Development

```bash
make install     # uv sync
make lint        # ruff check + ruff format --check
make format      # ruff format + ruff check --fix
make typecheck   # pyright
make test        # pytest with coverage gate
make check       # all of the above
```

## Limitations

- Linear-only. The board model is hard-coded to Linear states and
  labels — no Jira/GitHub Issues backend.
- Polling, not webhooks. At ~3 workers this is fine; past that, see
  the fan-out plan.
- One repo per `albedo run` invocation. To work with multiple repos in
  parallel, run `albedo` from each in its own shell (state and
  worktrees are scoped per `name` so they never collide). See
  [`docs/multi-project.md`](docs/multi-project.md).
- No auto-merge. A human always merges.
- Cost controls are token-based soft caps, not USD-based hard kill
  switches — observe usage on Anthropic's billing dashboard.

## License

MIT — see `pyproject.toml`.
