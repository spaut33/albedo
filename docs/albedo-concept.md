# AI Agent Orchestrator — Architectural Concept

## 1. Mission and scope

An autonomous AI-agent orchestrator on top of Claude Code that drives
development from task decomposition to a merge-ready PR. N local workers
pull tasks in parallel from a single Linear board, spawn a headless
`claude -p` process per task, work in isolated git worktrees, and open
PRs on GitHub. A human is in the loop only at two points: approving the
decomposition and approving the final PR.

### In scope (POC)
- Local execution (one machine, several processes)
- Linear as the sole state store
- Universal workers, role dispatch by column
- Multiple stacks via a per-repo contract
- Sub-agents (the Task tool) inside `claude -p` for read-heavy roles

### Out of scope (POC)
- Cloud deployment, containerization
- Auto-merging PRs
- Long-lived interactive Claude Code sessions
- Webhook integrations (polling only)
- Web UI (the Linear UI is enough)
- Tester as a separate role (the Coder runs tests)

---

## 2. High-level architecture

```
Linear board (shared state, columns + assignee-as-lock)
     │
     │ poll · claim · update (via Linear MCP)
     ▼
Orchestrator process (Python)
     ├─ Worker 1 ──┐
     ├─ Worker 2 ──┤  each: loop poll → claim → spawn `claude -p` → cleanup
     └─ Worker N ──┘
                   │
                   ▼ for each task
            Headless `claude -p`
                   │ working in:
                   ▼
            Git worktree (per task, branch=task/<issue-id>)
                   │ push, open PR (via GitHub MCP)
                   ▼
              GitHub PR ──► Human approval ──► merge ──► Linear: Done
```

Principles:
- **Linear = source of truth.** The orchestrator keeps no persistent
  per-task state locally (only metrics/budget at most).
- **Workers are identical and stateless across tasks.** Any of them can
  be killed and restarted.
- **A role is a prompt mode picked from the task's current column.** The
  worker does not know "I am a Coder"; it knows "this task is in Backlog
  → run Coder mode".
- **Multi-agent only exists inside a single `claude -p` session** (the
  Task tool, sub-agents) for parallel research/checks. At the
  orchestrator level, multi-agent is implemented as a shell loop.

---

## 3. Components

### 3.1 Orchestrator process
A Python daemon that launches and manages workers. It is itself
stateless with respect to tasks (everything lives in Linear). Locally
it stores only: metrics (cost, throughput) and active worktree paths.

Responsibilities:
- Spawn N workers at startup
- Worker health checks (heartbeat in a log file or via signals)
- Budget gate (cost limit before spawning `claude -p`)
- Graceful shutdown (let current tasks finish, refuse new ones)
- Worktree GC (clean up worktrees of removed branches)
- Approval watcher (strips the `draft` label from children once the
  decomposition is approved, see §13.1)

### 3.2 Worker
A loop running in a separate process or thread of the orchestrator.
See §5.

### 3.3 Linear board
External state. All workers read from and write to it (plus GitHub for
PRs).

### 3.4 Git worktree
An isolated working directory per task. Created on pickup, removed
after merge or cancellation. Branch: `task/<issue-id>`. Base branch
comes from the repo config.

### 3.5 Headless Claude Code
`claude -p "<prompt>" --allowedTools <list> --cwd <worktree>` — a
one-shot invocation per task. The process exits when done.

### 3.6 GitHub PR
Output. Opened by the Coder, reviewed by the Reviewer, merged by a
human.

---

## 4. Linear setup

### 4.1 Columns (workflow states)

| Column | Purpose | Pickup column? | Pickup role |
|---|---|---|---|
| Triage | Raw input from a human or decomposition input | ✅ | ARCHITECT |
| Backlog | Decomposed task, ready to code | ✅ | CODER |
| Review | PR is open, needs AI review | ✅ | REVIEWER |
| Awaiting approval | Human gate (after decomposition or after review) | ❌ | — |
| Done | Final state | ❌ | — |

We do not introduce a separate "In progress" column — a task being
worked on is identified by the presence of an assignee. This is visible
in the Linear UI and avoids extra columns.

### 4.2 Custom fields

| Field | Type | Default | Purpose |
|---|---|---|---|
| `attempts` | Number | 0 | How many times the Reviewer bounced the task. ≥3 → escalation |
| `repo` | Text | — | Which repo this task targets (when there is more than one) |
| `pr_url` | URL | null | Filled by the Coder after opening the PR |

### 4.3 Labels

- `size:S` / `size:M` / `size:L` — set by ARCHITECT during decomposition
- `stuck` — set on escalation (after 3 failed reviews)
- `needs-coordination` — Coder detected a file conflict with another in-flight task
- `blocked-external` — waiting on something outside the system (manual lift by a human)
- `draft` — child issue whose parent is still waiting on decomposition approval. Workers exclude these from polling. Stripped by the orchestrator after approval. See §13.1.
- `kind:decomposition` / `kind:final-pr` — marks the reason the issue is in Awaiting approval, so the human knows what they are approving

### 4.4 Agent users

Create N users in Linear: `agent-1`, `agent-2`, ..., `agent-N` (matching
the worker count, with some headroom). Each worker gets an `agent_id`
at startup. These are used as the assignee for claiming.

Linear API token is shared, with rights from these users. Separate
tokens are not required for the POC.

### 4.5 Workflow rules (Linear automation)

The minimum — everything else lives in orchestrator code:
- New issue in the project → automatically into Triage
- Status Done → auto-archive after 7 days (preserve free-tier limits)

---

## 5. Worker loop specification

### 5.1 Lifecycle

```
on_start():
  agent_id = "agent-{N}"
  log("worker {agent_id} started")

main_loop():
  while not shutdown_signal:
    if budget_exceeded():
      sleep(60); continue

    issue = poll_and_claim()
    if not issue:
      sleep(POLL_INTERVAL)  # 15-30 sec
      continue

    try:
      role = dispatch_role(issue.column)
      worktree = ensure_worktree(issue)
      result = spawn_claude(issue, role, worktree)
      handle_result(issue, result)
    except Exception as e:
      handle_failure(issue, e)
    finally:
      maybe_cleanup_worktree(issue)
```

### 5.2 Polling cadence

- Base interval: 20 seconds
- Jitter: ±5 seconds (so N workers do not hit Linear in lockstep)
- Backoff on rate limit: exponential, max 5 minutes

### 5.3 Claim protocol (assignee-based, atomic-enough)

```
poll_and_claim():
  # GraphQL query: issues in pickup columns, assignee = null, no `draft` label
  candidates = linear.query_unclaimed_in_pickup_columns()

  for issue in candidates:
    # Optimistic claim
    linear.update_issue(issue.id, assignee=self.agent_id)

    # Read-after-write verification
    sleep(0.5)  # let Linear propagate the write
    fresh = linear.get_issue(issue.id)
    if fresh.assignee == self.agent_id:
      return fresh  # claim succeeded
    # else — we lost the race, try the next candidate

  return None
```

### 5.4 Spawn protocol

```
spawn_claude(issue, role, worktree):
  prompt = build_prompt(issue, role, worktree)
  allowed_tools = TOOL_PERMISSIONS[role]
  mcp_servers = ["linear", "github"]  # all roles use both

  proc = subprocess.run(
    ["claude", "-p", prompt,
     "--cwd", worktree,
     "--allowedTools", allowed_tools,
     "--mcp-config", mcp_config_path],
    timeout=ROLE_TIMEOUT[role],  # 15-30 minutes
    capture_output=True
  )

  return parse_result(proc)  # exit code, stdout summary, side-effects detected
```

Important: success/failure is decided NOT by Claude's exit code but by
side effects: did the task move to the expected column, was a PR opened,
was a comment posted? Claude can exit 0 without doing the work — that is
a blocker, not a success.

### 5.5 Cleanup

- Worktree is removed when:
  - The PR is merged (a background GC polls GitHub)
  - The task is in Done or Cancelled
  - The worktree is older than N days with no activity (safety)
- Never auto-removed if there are unpushed commits.

---

## 6. Role dispatch

### 6.1 Dispatch table

| Source column | Role | Worktree | Allowed tools | Target on success | Target on blocker |
|---|---|---|---|---|---|
| Triage | ARCHITECT | shared (read-only repo) | Read, Bash(read), Linear MCP | Awaiting approval (kind:decomposition) | Triage + comment |
| Backlog | CODER | per-task | All (Edit, Bash, Linear MCP, GitHub MCP) | Review | Backlog + label `needs-coordination` |
| Review | REVIEWER | per-task (PR branch) | Read, Bash(tests only), Linear MCP, GitHub MCP | Awaiting approval (kind:final-pr) | Backlog + increment attempts |

### 6.2 Role boundaries (hard rules across all roles)

- Never merge a PR
- Never move someone else's task ("mine" = assigned to me)
- Never modify `attempts` by hand — only via the Reviewer
- Never delete another worker's worktree
- Never commit directly to the base branch

---

## 7. Roles in detail

### 7.1 ARCHITECT

**Trigger:** issue in Triage with no assignee.

**Goal:** split the task into 2–7 implementable child issues, send the
parent to a human approval gate.

**Behavior:**

Pre-flight: if this issue already has child issues (parent = current
issue), then a previous decomposition was rejected by a human. Archive
all existing children via Linear MCP before starting over.

1. Read the issue + comments
2. If the description is ambiguous — comment a clarifying question on
   the issue, leave it in Triage, exit. **No guessing.**
3. Otherwise, in parallel (via the Task tool, see §9.1), explore the
   repo: related modules, tests, recent commit history
4. Decompose:
   - 2–7 child issues
   - Each — implementable in one PR (~<500 LOC diff)
   - Each has explicit acceptance criteria (a checklist in the
     description)
   - Sizes: S (<2h of agent work), M (2–6h), L (split further)
5. Create child issues via Linear MCP:
   - Title: imperative ("Add X", "Refactor Y")
   - Description: context + AC + "parent: LIN-XXX"
   - Parent link
   - Initial column: Backlog
   - Label `size:{S|M|L}`
   - Label `draft` — children are blocked from pickup until the human
     approves the decomposition (see §13.1)
6. Comment on the parent: list of created issues, brief rationale,
   recommended order
7. Move the parent to Awaiting approval with label `kind:decomposition`

**Sub-agents:** yes, for parallel research (see §9.1).

**Does not:** write code, open PRs, create more than 7 sub-tasks (if
the task is bigger — comments "needs epic split" and exits).

**Failure modes:**
- Cannot understand the task → posts a clarifying comment, leaves it in
  Triage
- Task is too large → "needs epic split" comment, leaves it in Triage
  with label `blocked-external`

### 7.2 CODER

**Trigger:** issue in Backlog with no assignee.

**Goal:** implement the task, open a PR, leave it in Review.

**Behavior:**

Pre-flight:
- `pwd` matches the worktree
- `git fetch origin && git rebase origin/<base>` — get up to date
- Query sibling issues (same parent, assignee != null, column ∈
  {Backlog, Review}) → if they plan to touch the same files (see
  §7.2.1) — STOP, label `needs-coordination`, back to Backlog, exit

Implementation:
1. Internal plan (does not write to Linear)
2. Implementation in logical commits (Conventional Commits style)
3. Tests for the new behavior (not just the happy path)
4. `make test`, `make lint`, `make typecheck` — until green. Missing
   targets are skipped.
5. Scope discipline: incidental bugs/improvements → separate Linear
   issues, do not bundle them into the current PR

Wrap-up:
1. `git push -u origin <branch>`
2. Open the PR via GitHub MCP:
   - Title: `<issue_id>: <title>`
   - Body: summary + "Closes <issue_id>" + AC checklist with check
     marks
   - Base: base branch from the repo config
3. Write `pr_url` to the custom field
4. Comment on Linear with the PR URL
5. Move to Review

**Sub-agents:** usually none. One optional sub-agent up front to "find
similar patterns in the repo" — its result lands in the main context.
After that the main agent proceeds sequentially.

**Does not:** scope creep, merge its own PR, change other tasks.

#### 7.2.1 Sibling-conflict check (details)

Before starting implementation the Coder:
- Figures out which files it plans to touch (from AC + a quick grep of
  the repo)
- Asks Linear: issues with the same parent, assignee != null, column ∈
  {Backlog, Review}
- For each, reads the description + latest comments + (if a PR exists)
  the changed files
- If the file sets overlap → bail with `needs-coordination`, the human
  decides the order

This is the first line of defense. The second is the final `git rebase`,
which surfaces conflicts if any slipped through.

### 7.3 REVIEWER

**Trigger:** issue in Review with no assignee, with `pr_url` populated.

**Goal:** issue an APPROVE / REQUEST_CHANGES verdict.

**Behavior:**
1. Find the PR (by `pr_url` on the issue)
2. Read the diff, CI status, AC from the issue
3. Read `attempts`. If ≥ 3 → ESCALATE (see below)
4. In parallel (via the Task tool, see §9.1) run 4 checks in isolated
   contexts:
   - **AC verification** — is each AC bullet actually covered by the
     diff? With line refs.
   - **Test quality** — do tests actually exercise the new behavior, or
     just import it?
   - **Correctness/security** — obvious bugs, regressions, security
     smells?
   - **Style consistency** — does the diff match the style of the 2–3
     nearest files in the repo?
5. Synthesize findings → decision
6. Action:
   - **APPROVE** → GitHub review = APPROVE with a brief comment
     ("auto-review: AC verified — tests cover X, Y, Z"), Linear →
     Awaiting approval with label `kind:final-pr`
   - **REQUEST_CHANGES** → line-anchored comments on specific lines on
     GitHub, summary comment on Linear, increment `attempts`, Linear →
     Backlog (assignee unset)

**ESCALATE (`attempts >= 3`):**
- Comment on the issue and PR with the history of all prior reviews
- Label `stuck`
- Linear → Awaiting approval (a human takes over)
- Exit

**Sub-agents:** yes, four parallel checks (see §9.1).

**Does not:** push commits to someone else's PR (only bounce back),
merge, approve without item-by-item AC verification.

---

## 8. Prompt template

### 8.1 Common prefix structure

Passed to every `claude -p` regardless of role. `{}` placeholders are
filled by the orchestrator from the Linear API + config.

```
You are agent-{agent_id} in an autonomous orchestrator working on
Linear issue {issue_id} ("{title}"), currently in column "{column}".
Your role for this task: {role}.

Working directory: {worktree_path}
  - This is a git worktree for branch {branch}, base {base_branch}
  - Repo: {repo_name}
  - Repo conventions: see ./CLAUDE.md
  - Repo build/test contract: ./Makefile (targets: test, lint, typecheck, build)
  - Skip absent Make targets silently.

Tools available: {allowed_tools}
MCP servers: linear, github

Issue:
  ID: {issue_id}
  Title: {title}
  Description:
{description_indented}
  Acceptance criteria:
{ac_bullets}
  Parent: {parent_id_or_none}
  Attempts so far: {attempts}

Universal rules (all roles):
- Comment on Linear {issue_id} with a brief summary before exiting.
- On success: move to "{target_column_on_success}" via Linear MCP.
- On blocker: comment "BLOCKED: <reason>" and move to "{target_column_on_blocker}".
- Never merge PRs. Never modify other in-flight issues. Never edit attempts.
- All Linear/GitHub I/O goes through MCP. No curl, no manual API calls.
- Time budget: ~{role_timeout} minutes total wall clock. Wrap up if approaching.

[Role-specific block follows below]
```

### 8.2 Role-specific block structure

Appended below the common prefix. Contains:
- A clear description of the role's goals
- The step-by-step protocol (as in §7)
- When to use the Task tool, and for what (see §9.1)
- Which custom-agents to invoke if applicable (see §9.2)
- Failure modes — how to exit cleanly

For examples see the prior ARCHITECT/CODER/REVIEWER discussions. In the
implementation, store templates in `prompts/<role>.md`; the orchestrator
just concatenates prefix + role block + substitutes variables.

### 8.3 Variable substitution

Plain `str.format(**vars)` or Jinja2. All variables are required —
missing ones blow up loudly, never silently.

Variable sources:
- `agent_id`, `worktree_path` — orchestrator
- `issue_id`, `title`, `description`, `column`, `attempts`, `parent_id` — Linear
- `repo_name`, `branch`, `base_branch` — repo config + naming
  conventions
- `target_column_on_success`, `target_column_on_blocker`,
  `allowed_tools`, `role_timeout`, `role` — dispatch table

---

## 9. Multi-agent at the `claude -p` level

### 9.1 Task tool usage (sub-agents inside one session)

Principle: a sub-agent is justified when (a) the subtask is independent,
(b) its summary is shorter than the raw input, (c) an isolated context
gives a qualitatively better check.

**ARCHITECT — parallel research:**
- Sub-agent A: "read the modules related to topic X, return a summary
  ≤ 200 words"
- Sub-agent B: "read the existing tests for topic X, return a coverage
  map"
- Sub-agent C: `git log --since="1 month ago" -- <related_paths>` +
  summary of recent intent

The parent gets 3 compact summaries instead of 5K tokens of raw files
and can decompose with a complete picture.

**REVIEWER — parallel checks:**
- Sub-agent 1: AC verification (each item → diff lines)
- Sub-agent 2: test quality (do tests actually exercise the new
  behavior)
- Sub-agent 3: correctness/security findings
- Sub-agent 4: style consistency vs nearby files

The parent synthesizes → APPROVE / REQUEST_CHANGES. Isolated contexts
yield independent opinions, not blurred-together "general overview".

**CODER — almost never:**
- Optionally one sub-agent up front: "find 2–3 similar patterns in the
  repo, return links" → result into the main context
- After that, Coder works sequentially. Implementation is stateful;
  sub-agents only get in the way.

### 9.2 Custom agents (`.claude/agents/`)

Live in the project repo (not in the orchestrator). Reusable prompt
templates with predefined `--allowedTools`. Loaded automatically by
Claude Code.

POC minimum:

**`.claude/agents/ac-verifier.md`** — takes an issue ID and a PR diff,
returns an "AC met?" checklist with line refs. Used by both Coder
(pre-PR self-check) and Reviewer.

**`.claude/agents/pattern-finder.md`** — "find 2–3 similar places in the
repo and return links + a brief description of conventions." Used by
the Coder before starting.

Later, as roles stabilize:
- `test-coverage-checker.md`
- `security-smells.md`
- `style-vs-neighbors.md`

Written as plain Markdown with YAML frontmatter (Claude Code custom
agent format). They contain the sub-prompt itself plus a list of
allowed tools.

---

## 10. Per-repo contract

Every repo the orchestrator serves must provide:

### 10.1 Required files

```
<repo>/
├── CLAUDE.md                      # architecture, conventions, gotchas
├── Makefile                       # standardized targets
├── .claude/
│   ├── settings.json              # tool permissions, hooks
│   └── agents/
│       ├── ac-verifier.md
│       └── pattern-finder.md
└── .orchestrator/
    └── config.yaml                # base branch, repo-specific overrides
```

### 10.2 Makefile targets

| Target | Behavior | Required? |
|---|---|---|
| `test` | Run all tests, exit 0/non-0 | ✅ |
| `lint` | Linter, exit 0 if clean | ✅ |
| `typecheck` | Type checker (where applicable) | optional |
| `build` | Build, exit 0 if ok | optional |
| `format` | Auto-format (Coder may invoke before PR) | optional |

Missing targets are skipped silently. The agent must NOT try to "guess"
a command — only Make.

### 10.3 `CLAUDE.md` content (recommended sections)

- What this repo is, in one paragraph
- Entry points (where `main` is, how to run locally)
- Key modules and their purpose
- Conventions for commits, branches, PRs
- Gotchas and pitfalls (what NOT to do)
- Where tests live, how they are structured

### 10.4 `.claude/settings.json`

Per-repo permissions and safety rails. Minimum:
- Forbid writes to certain paths (DB migrations, secrets)
- Auto-approve standard tools

### 10.5 `.orchestrator/config.yaml`

```yaml
base_branch: main
worktree_root: /tmp/orchestrator-worktrees  # where to put worktrees (opt.)
budget_per_task_usd: 5  # cap per task
required_checks:
  - test
  - lint
optional_checks:
  - typecheck
  - build
```

The orchestrator reads this file when picking up a task (by the `repo`
custom field).

---

## 11. Git worktree management

### 11.1 Naming

- Branch: `task/<issue-id>` (e.g. `task/LIN-142`)
- Worktree path: `<worktree_root>/<repo_name>-<issue-id>`
- `<worktree_root>` comes from `config.yaml`, default
  `~/.orchestrator/worktrees/`

### 11.2 Lifecycle

```
ensure_worktree(issue):
  path = compute_worktree_path(issue)
  if exists(path):
    return path  # already exists (e.g. Reviewer picks up after Coder)

  cd <main_repo_clone>
  git fetch origin
  git worktree add <path> -b task/<issue-id> origin/<base_branch>
  return path
```

When the Reviewer picks up — the worktree already exists from the
Coder, and is reused (it holds the fresh code on the PR branch).

### 11.3 Cleanup

Background GC every N minutes:
- Iterate all worktrees
- For each: look up the corresponding Linear task
- If the task is in Done or Cancelled, and there are no unpushed
  commits → `git worktree remove`
- If the worktree is older than 7 days → log a warning (do not
  auto-remove)

---

## 12. Failure handling

### 12.1 Attempts counter

The `attempts` custom field is incremented by the Reviewer on every
REQUEST_CHANGES. We do not reset it — it is persistent for the life of
the task.

### 12.2 Escalation

When `attempts >= 3` the Reviewer skips the normal bounce and:
- Posts the full history of attempts on the issue (what each Reviewer
  feedback was)
- Posts the same on the PR
- Adds label `stuck`
- Moves to Awaiting approval

A human takes over. After manual intervention, `attempts` can be reset
to 0 and the task returned to Backlog.

### 12.3 Blocked tasks

Three kinds of blocks:
- `needs-coordination` (Coder, sibling conflict) — another agent is
  working on the same files. Returns to Backlog. When the other one
  finishes, the task is picked up again and the conflict is gone.
- `blocked-external` — the task is waiting on something outside the
  system (a human reply, an external service). Workers do not pick it
  up; it waits for the label to be lifted manually.
- `stuck` — escalated after 3 failed reviews. See above.

### 12.4 Stale claims

A worker might crash, leaving a task with assignee=agent-X but no one
actually working on it.

Detection:
- Heartbeat: every worker updates a timestamp in a local file
  `state/agent-<id>.heartbeat` once per minute
- On startup the orchestrator checks: are there Linear tasks assigned
  to agent-X whose heartbeat is older than 5 minutes? → unassign and
  leave them in their current column. Logged as stale-claim recovery.

Additionally: timestamp in a Linear comment on pickup. If a task is
under an agent's assignee but more than `role_timeout × 1.5` has passed
without changes in Linear or git → forced un-claim (protection against
hung processes).

---

## 13. Approval mechanisms

Two distinct gates with distinct mechanics. Both use native platform
operations — no magic comments, no custom buttons, no special triggers.

### 13.1 Decomposition approval (after ARCHITECT)

**State:** parent in Awaiting approval with label `kind:decomposition`,
children created by the Architect in Backlog with label `draft`
(workers ignore them).

**Human action:**
1. Open the parent in Linear, read its rationale comment
2. Follow the links to the child issues, check AC and order
3. **Approve:** move the parent to Done. Semantics: the parent task is
   complete, and its work was the decomposition.
4. **Reject:** move the parent to Triage with a feedback comment.

**What happens automatically after approve (parent → Done):**
- The orchestrator's approval watcher detects an issue with label
  `kind:decomposition` transitioning to Done
- It strips the `draft` label from all of that parent's children
- The children become visible to polling — Coders pick them up and
  start
- Each child later goes through its own Coder → Review → final approval
  cycle with its own PR

**What happens after reject (parent → Triage):**
- On re-pickup, the Architect sees existing children in pre-flight and
  archives them (see §7.1)
- Produces a fresh decomposition incorporating the feedback

**Editing the plan before approval (Linear-native):**

Children sit untouched in Triage until the parent is approved, so the
human has free reign:

- **Edit a child:** open it in Linear, change title / description /
  acceptance criteria / estimate / `blocks` relations as needed. The
  Coder will read whatever is in the issue at pickup time.
- **Drop a child:** add the `cancelled-by-human` label to it. On parent
  approval, housekeeping moves labelled children to Canceled instead of
  releasing them. They are also in `EXCLUDE_LABELS`, so even an
  accidentally released one would not be picked up.
- **Add a child:** create a new Triage issue, set its parent to the
  decomposition parent, optionally add `blocks` relations to existing
  siblings. Housekeeping releases anything in Triage with that
  `parent_id` — manual additions get released alongside Architect
  output.

The Architect's rationale comment will go stale after manual edits.
That is acceptable — the source of truth is the children themselves.

**No PR is created here.** Decomposition approval is permission to
start coding, not the coding itself. PRs come later — one per child —
from the Coders.

### 13.2 Final PR approval (after REVIEWER)

**State:** Coder opened a PR, Reviewer approved it on GitHub, issue is
in Awaiting approval with label `kind:final-pr`. **The PR already
exists.**

**Human action — entirely on GitHub, not in Linear:**
1. Open the PR (URL in the `pr_url` field or in a Linear comment)
2. Read the diff, AI review, CI status
3. Approve = click Merge.

**What happens automatically after merge:**
- Linear's native GitHub integration moves the linked issue to Done.
  This is a **built-in Linear feature**, enabled in Settings →
  Integrations → GitHub. No code on our side.
- Worktree GC notices the issue is in Done → removes the worktree (see
  §11.3)
- The branch on GitHub is deleted automatically if the repo has
  auto-delete merged branches enabled

**If the human does not want to merge:**
- Closes the PR without merging on GitHub
- Returns the Linear task to Backlog or Triage with comments
- The task enters a new cycle with the same AC; `attempts` is reset
  manually if needed

### 13.3 Why this design

Each gate uses the platform's native operation:
- Decomposition is approved in Linear (the native place for managing
  tasks)
- The PR is approved on GitHub (the native place for review/merge)

No custom buttons, no magic comments, no separate dashboards. The human
does what they would do in a normal workflow anyway.

The only approval-specific code we own is the approval watcher (strips
`draft` from children when decomposition is approved). The final merge
flows through the native Linear ↔ GitHub integration.

---

## 14. Cost controls

### 14.1 Budget gates

- **Per task:** soft cap from `config.yaml` (e.g. $5 per task). Does
  not block execution but is logged and counted toward the daily total.
- **Per agent per day:** hard cap (e.g. $50/agent/day). On overrun the
  agent stops picking up new tasks until the end of the day.
- **Global per day:** hard cap on the orchestrator (e.g. $200/day). On
  overrun all workers sleep.

We compute from Anthropic API usage (available via billing or logged
tokens × pricing). Stored in local SQLite or just a JSON file.

### 14.2 Soft limits

- Before spawning `claude -p` — check the daily budget
- Log cost after each task (for analytics)
- Stdout alert if daily cost exceeds 50% of cap (early warning)

---

## 15. Orchestrator file layout

```
orchestrator/
├── pyproject.toml
├── README.md
├── config/
│   ├── orchestrator.yaml          # global config (agents, budget, repos)
│   └── linear-mcp.json            # MCP server config for Linear
├── prompts/
│   ├── common-prefix.md.tmpl
│   ├── architect.md
│   ├── coder.md
│   └── reviewer.md
├── src/
│   ├── orchestrator/
│   │   ├── __init__.py
│   │   ├── main.py                # entrypoint, spawns workers
│   │   ├── worker.py              # main loop
│   │   ├── linear_client.py       # wrapper over Linear MCP/GraphQL
│   │   ├── github_client.py       # wrapper over GitHub
│   │   ├── claim.py               # claim protocol
│   │   ├── dispatch.py            # column → role mapping
│   │   ├── prompt_builder.py      # prompt assembly
│   │   ├── claude_runner.py       # spawn `claude -p`
│   │   ├── worktree.py            # git worktree CRUD
│   │   ├── budget.py              # cost tracking
│   │   ├── approval_watcher.py    # strip `draft` label after decomposition approve
│   │   └── recovery.py            # stale claim handling
├── tests/
└── state/
    ├── agent-1.heartbeat
    ├── agent-2.heartbeat
    └── budget.db                  # SQLite or JSON
```

---

## 16. Implementation phases

Phases with explicit exit criteria — after each one you can stop and
verify it works.

### Phase 1 — Single worker, hardcoded issue
- One worker, task ID hardcoded in code
- CODER mode only
- Worktree created manually before the run
- Goal: confirm that `claude -p` with MCP actually reads Linear, makes
  changes in the worktree, and opens a PR

### Phase 2 — Real loop, Backlog → Review
- The worker polls Linear itself
- Claim via assignee
- Ensure worktree automatically
- Coder pipeline end-to-end
- Goal: a single worker drives a task Backlog → Review without manual
  intervention

### Phase 3 — Reviewer
- Add the REVIEWER mode
- Cycle Review → (Backlog | Awaiting approval)
- `attempts` counter
- Escalation at `attempts >= 3`
- Goal: a task can go through the full cycle Backlog → Review → Backlog
  → Review → Awaiting approval

### Phase 4 — Architect + decomposition gate
- Add the ARCHITECT mode
- Triage → Awaiting approval
- Children created with label `draft`, excluded from polling
- Approval watcher: on parent transition to Done with label
  `kind:decomposition`, strips `draft` from all children
- Pre-flight in Architect: archive existing children on reject and
  re-pickup
- Sub-agents (Task tool) for research
- Custom agents `.claude/agents/ac-verifier.md`,
  `.claude/agents/pattern-finder.md`
- Goal: full decomposition flow from Triage to Coders running on
  children after approval

### Phase 5 — Multi-worker, race testing
- Run 2–3 workers in parallel
- Verify the claim protocol under load
- Sibling-conflict check in the Coder
- Stale claim recovery
- Cost gates
- Goal: 2–3 workers pull tasks in parallel without conflicts

### Phase 6 — Hardening
- Logging, observability (statusline or a simple dashboard)
- Graceful shutdown
- Worktree GC
- Persistent budget tracking
- Daily archive of Done issues (Linear free tier)

---

## 17. Open questions / decisions deferred

These are deliberately left to implementation time — to be decided as
we go without blocking the start.

1. **Which Python framework for workers?** `asyncio` (one process, N
   tasks) vs `multiprocessing` (N processes). For the POC,
   multiprocessing is simpler (isolation, easy to kill one worker).
   Async — if N grows past 10.

2. **Where to store cost data?** SQLite (if we plan analytics) vs
   append-only JSONL (simpler). POC — JSONL.

3. **Logging.** stdout per worker + file rotation. Structured (JSON)
   for downstream parsing.

4. **How to survive an orchestrator restart mid-task?** On startup,
   check for stale claims (see §12.4). The worker has no persistent
   state beyond the heartbeat — that is fine.

5. **When to introduce Tester as a separate role?** When the Coder can
   no longer keep up with running tests inside its own loop (slow
   integration/e2e). Until then, the Coder does it all.

6. **Linear webhooks → push notifications** instead of polling? More
   convenient but harder (needs a public endpoint). For a local POC,
   polling. Move to webhooks for cloud deployment.

7. **Multi-repo support.** In the POC, the orchestrator serves a single
   repo (the `repo` custom field is then unnecessary). Multi-repo —
   through the same `config.yaml` with a list of repos and a `repo`
   field on the issue.

8. **What goes in the agent's commit message?** Conventional Commits
   with the issue ID. For example: `feat(LIN-142): add user filter to
   admin panel`. A stable convention makes changelog generation easy.

---

## 18. Definition of Done (for the orchestrator as a project)

The POC is considered ready when:
- 2–3 workers start with `albedo`
- A Linear issue can be created in Triage
- Within 30 minutes, with no human intervention:
  - the issue is decomposed (ARCHITECT)
  - the parent moves to Awaiting approval, the human approves it
  - child issues are picked up by Coders automatically
  - PRs are opened, AI reviewed
  - approved issues land in Awaiting approval (final)
  - the human merges the PR, the issue → Done
- Cost is logged, budget overrun stops spawning
- Restarting the orchestrator recovers stale claims
- No race conditions across 2–3 parallel workers over an hour of
  continuous operation
