# Roadmap

Open architectural and product gaps surfaced from external research. Not
a prioritised backlog — these are deliberately deferred items kept here
so they are not lost. Each entry records the problem, why it matters,
roughly what it would take, and when it becomes load-bearing.

Already on the board:

- **AI-65** — Restructure decomposition schema for harness-grade
  grounding (`files_to_touch`, `relevant_symbols`, structured
  `context`/`implementation_notes`). Covers items 1 and 4 of the Red Hat
  audit.

---

## From Anthropic — *Scaling Managed Agents* (decoupling brain from hands)

### 1. Brain/hands coupling

**Status:** by design for the local POC. **Blocker** for any cloud /
multi-tenant / production-tenant deployment.

**Problem.** `claude -p` runs *inside* the worktree (`--cwd`) with direct
filesystem access and direct env-vars carrying tokens. Harness and
sandbox are one process with one security boundary. Anthropic's
architecture exits the harness from the container and exposes the
container only as `execute(name, input) → string`. We are the opposite.

**Consequences we already feel.**
- Crash of `claude -p` is indistinguishable from a tool-call failure or
  from "Claude voluntarily exited without doing the work" — we detect
  success only by side-effects (PR opened? column moved?), see
  [albedo-concept.md §5.4](albedo-concept.md). This is an
  acknowledged workaround, not a fix.
- Cannot replace the sandbox without restarting Claude.
- Cannot put the harness on a different machine while keeping the
  worktree local.

**What it would take.**
- A thin tool-server inside each worktree (HTTP / stdio JSON-RPC) that
  exposes `execute(name, input) → string` to a remote harness.
- The harness becomes a separate process (potentially on another host)
  that talks to the worktree only over that interface.
- Workers stay local for a while; the path that opens up is "harness on
  a small fleet, worktrees in customer VPC".

**When it bites.** The first paying tenant who says "I cannot give
Anthropic outbound network access from my repo's host". Until then,
defer.

---

### 3. Session granularity is task-level, not event-level

**Status:** acceptable for current task lengths (~15–30 min spawns).
Tightens as tasks get longer.

**Problem.** Linear stores task-lifecycle events (state moves, comments,
`pr_url`, `attempts`) — but not the per-token / per-tool-call event
stream inside one `claude -p` spawn. If Claude dies 12 minutes into a
15-minute spawn, we restart the task from scratch. The reasoning context
is gone; only the worktree's git history survives as an accidental
event log.

Anthropic's design: `getEvents()` over a durable append-only log with
arbitrary harness-side transforms (rewinding, slicing, reorganisation
for prompt-cache hits) before each inference. Our analogue —
"User feedback" comments re-read on the next spawn — is *much*
coarser.

**Consequences.**
- A long-running CODER that hits a flaky test on minute 14 of a 15-minute
  budget restarts the whole task next attempt. The work is not lost
  (commits are pushed if any), but the chain of reasoning is.
- Cannot do mid-task interventions ("wait, you misread the AC, here is a
  correction") at sub-task granularity. Best we can do is wait for the
  spawn to finish and use the comment-feedback loop on the next pass.

**What it would take.**
- Stop relying on `claude -p` as a black box. Either:
  - Build a custom harness over the Anthropic SDK that streams events
    out to durable storage (SQLite, JSONL, Linear comments, anything),
    *and* can resume from that storage on next invocation. Big lift.
  - Or, smaller: emit structured progress comments on a heartbeat from
    inside a long-running prompt (require Claude to post a
    `PROGRESS:` marker every N minutes that the worker ferries to
    Linear). Cheap, partial, no resume — but at least no silent loss.

**When it bites.** As soon as we want tasks longer than ~30 min wall
clock, or as soon as we want mid-task user steering. Related to item
#5 below (custom harness).

---

### 4. TTFT (time to first token) not optimised

**Status:** known cost paid every spawn. Not blocking; visible on the
billing dashboard.

**Problem.** Each `claude -p` invocation pays a cold start that includes:
- Claude SDK + CLI boot
- `npx -y @modelcontextprotocol/server-github` (re-downloads on cold
  npm cache, otherwise unpacks)
- `git fetch` + `git rebase origin/<base>` inside the worktree
- Re-reading the issue body and comments from Linear

Anthropic reports a 60% p50 / 90% p95 reduction in TTFT after
provisioning hands on-demand and keeping the harness warm. We are
paying their pre-decoupling number on every task.

**What it would take, in cost order.**
- *Cheap:* persistent npm cache directory; warm one shared worktree per
  repo for read-only ops (Architect already only reads).
- *Medium:* MCP server warm pool — keep a pre-spawned GitHub MCP per
  worker, reattach on each Claude spawn instead of re-spawning.
- *Bigger:* persistent Claude session via `--resume` for the same task,
  rather than fresh spawn each time. Couples to item #3.

**When it bites.** Once we run >50 tasks/day per worker. Below that,
absolute cost is small even at high p95. Don't optimise prematurely;
record the numbers first.

---

### 5. Stock `claude -p`, not a custom harness

**Status:** correct choice for POC. Becomes a ceiling later.

**Problem.** We don't own the harness. Context engineering, prompt
caching, compaction, sub-agent dispatch — all of it is whatever
`claude -p` does today. We have only two control points: input
(prompt + MCP config) and output (stdout). Anthropic's whole article
hinges on owning the harness so they can reorganise events between
inferences for cache hits and run arbitrary context strategies.

**What we lose.**
- No way to do `getEvents() → transform → next inference` (item #3 is
  downstream of this).
- Cannot prefetch known context into the prompt cache; whatever ordering
  `claude -p` does is what we get.
- Cannot insert programmatic checkpoints ("after each tool call, do
  X").

**When it bites.** The day we hit a context-engineering wall on a
specific role (most likely REVIEWER on large diffs, where parallel
sub-agents already help but only because the Task tool exists at all).
At that point, the smallest viable replacement is a thin Python harness
over the Anthropic SDK that mimics `claude -p`'s tool-call loop and
adds the one knob we need.

---

### 6. Observability: harness vs sandbox failures

**Status:** known weakness of the coupled design. Detection by
side-effects ([albedo-concept.md §5.4](albedo-concept.md)) is a
workaround.

**Problem.** "Claude crashed", "Claude timed out", "GitHub MCP failed",
"Claude exited cleanly without doing the work" are all logged as the
same thing today: the spawn ended, no `PR:` line, mark BLOCKED. We
cannot route alerts ("page someone — npm broke") differently from "this
issue's AC is just bad".

**What it would take.**
- Capture structured tool-call telemetry from the spawn (stdout already
  carries tool-use events in `--output-format=stream-json`; we should
  parse them).
- Distinguish in the worker log: `tool_call_failed{tool=X}`,
  `model_exit{reason=...}`, `harness_timeout`. Three different counters,
  three different alerts.
- Tie failures back to the structural fault domain (sandbox / harness /
  external service) for dashboards.

**When it bites.** First time we get paged at 2am for "tasks failing"
without a clear root cause. Until we run continuously for a tenant,
this is just hygiene.

---

## From Red Hat — *Harness Engineering* (not covered by AI-65)

### Standalone repository impact map as a separate reviewable artifact

The article makes the impact map a separate artifact the human approves
*before* tasks are even created — so structural mistakes ("agent picked
the wrong module") get caught before they cascade into N children.

We collapse this: research happens inside the same ARCHITECT spawn that
emits children, and the human only sees the resulting decomposition. If
the Architect picked the wrong subsystem, the human discovers it via N
bad children, not one wrong map.

**Smallest viable change.** Have the Architect post a richer rationale
comment on the parent — not just "I split because X" but the actual
modules it touched, the symbols it found, and what each child will
target — so the approval gate effectively reviews the impact analysis,
not just the task list. Approval semantics stay the same. This is a
prompt-only change once AI-65 lands (since the schema will already carry
`files_to_touch` / `relevant_symbols` per child — we just need them
summarised at the parent level too).

### LSP-grade grounding

`mcp-servers.json` ships Linear + GitHub. Architect's research is
grep / Read inside Task sub-agents — fine for files, weak for "find all
callers of X" or "verify the actual signature of this trait". An LSP
MCP server (one exists, `github.com/jonrad/lsp-mcp` or similar) would
make `relevant_symbols` from AI-65 actually verifiable rather than
descriptive.

**When it bites.** If AI-65 lands and the Architect's `relevant_symbols`
list turns out to be hallucinated half the time, that's the signal.

### No harness-feedback loop

Article: *"trace output errors back to input constraints and fix the
harness"*. Today, when Reviewer rejects → Coder retries → escalation
fires, **none of that signal flows back into the prompts or templates**.
Each task starts from the same prompt. The harness can't learn from
systematic failure modes (e.g. "Architect under-specifies tests in
module X every time").

**Smallest viable change.** When a task escalates (`stuck` label), an
operator-side checklist: was the root cause (a) bad AC from a human, (b)
bad decomposition by Architect, (c) bad implementation by Coder, (d) a
gap in the prompt for one of those roles? Item (d) generates a prompt
PR; the others don't. No automation — just a habit. Free.

### Runtime / observability surface beyond CI

Reviewer already reads CI status (AI-57 / AI-58 added the
workflow-runs / jobs / logs / redispatch primitives, and
[reviewer.md:101-103](../src/albedo/_data/prompts/reviewer.md) instructs
`REQUEST_CHANGES` on red CI). Still missing:
- Deployment / staging logs — "did this code actually run anywhere?"
- Runtime metrics — "did the new endpoint regress p95?"
- Flake-aware retry — `ci_redispatch.py` exists but Reviewer does not
  yet use it; flaky CI today still produces `REQUEST_CHANGES`.

The flake retry is a tractable first step: extend the Reviewer prompt to
diagnose flake vs real failure from logs, and on flake, redispatch and
re-poll instead of bouncing. The runtime/staging part is a much bigger
ask and not on the near horizon.
