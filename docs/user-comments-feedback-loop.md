# User comments feedback loop

How human comments on Linear issues reach the agents, when re-dispatch
fires, and what the edge cases are.

## TL;DR

Drop a comment on any Linear issue. On the next pickup by an agent
(Architect / Coder / Reviewer), your comment is read, treated as
**authoritative human guidance** (overrides the original description on
conflict), and factored into the work. If your comment shifts scope/AC
enough to invalidate the issue body, the agent rewrites the body via the
orchestrator (an audit comment with the previous body is posted
automatically).

## When your comment will be read

| Issue is in… | Behaviour |
|---|---|
| **Triage** (no parent, no gating label) | Architect picks it up on the next poll. Comment is in the prompt. |
| **Backlog** (no gating label) | Coder picks it up on the next poll. Comment is in the prompt. |
| **Review** | Reviewer picks it up on the next poll. Comment is in the prompt. |
| **Triage** / **Backlog** with `awaiting-human-reply` (architect or coder asked a clarifying question) | Worker pool ignores the issue until your comment arrives. Within ~30s the label is stripped; on the next poll Architect (or Coder) re-runs with your answer in the prompt. |
| **Awaiting approval** with `kind:final-pr` (PR ready, awaiting human merge) | Within ~30s the issue moves back to **Review**; Reviewer re-checks PR + your comment. |
| **Awaiting approval** with `kind:decomposition` (architect proposal) | Within ~30s the issue moves back to **Triage**, existing children are **archived**, Architect re-decomposes with your feedback. |
| **Awaiting approval** with `stuck` (REVIEWER hit max attempts) | Within ~30s the `stuck` label is stripped and the issue moves to **Backlog**; Coder retries. |
| **In Progress** (an agent is actively working on it) | Comment is read on the **next** pickup, not now — the running spawn is not interrupted. |
| **Done** / archived | Not read. Reopen the issue if you need a re-run. |

### `awaiting-human-reply` — when an agent asks you a question

When Architect (in Triage) or Coder (in Backlog) finds the
description/AC genuinely ambiguous, it posts a `**agent-N**: BLOCKED:
<question>` comment and the orchestrator adds the
`awaiting-human-reply` label to the issue. That label sits in the
worker's `EXCLUDE_LABELS` list, so:

- The issue stays in its column (Triage or Backlog) but **is not
  re-picked**. No looping, no spurious re-runs.
- Whoever you reply to (the agent's question) — your comment counts as
  a user comment.
- The redispatch loop sees the new user comment, strips the
  `awaiting-human-reply` label, and the next poll picks the issue up
  with your answer in the "User feedback" prompt block.

This applies only when the agent emitted an actual `BLOCKED:` line
(i.e. asked a question). Pure errors (claude crash, missing PR marker
without a `BLOCKED:` line) keep the issue **re-pickable** so transient
failures self-heal without human input.

## How "user vs bot" is decided

Filtering is **by author identity**, not by content recency. The agent
sees the **full history** of human-authored comments (oldest first).
"User" means anything that survives this filter:

1. Drop the comment if `author_id` is in the precomputed `bot_user_ids`
   set. The set is built at supervisor startup by calling Linear's
   `viewer()` on every per-agent token plus the shared key — so we know
   exactly who the bot is via the API.
2. Drop the comment if the body starts with `**agent-N**:` (bold
   asterisks + colon). Defensive backstop for cases where `bot_user_ids`
   discovery is incomplete.

Both rules are independent of comment age. A six-month-old comment from
a human is still in the prompt the next time the issue is picked up.

## @-mentioning the bot

You can @-mention the bot in a Linear comment ("@agent-1 please rename
function X to Y") and it will land in the agent's prompt verbatim. The
filter looks at `author_id` — yours, not the bot's — so your message
passes through. Linear's native @-mention syntax does not start with
`**agent-N**:`, so the prefix backstop doesn't trip either.

The one behaviour worth knowing: if you type `**agent-N**:` (bold
asterisks + colon) literally at the start of a comment, it's filtered
out. You'd have to be deliberately mimicking the bot's own format —
Linear's @-mention UI never produces this.

## Issue-body rewrites

If a comment changes scope/AC enough, the agent may emit a sentinel
block in its final response:

```
<<<ISSUE_UPDATE>>>
<full replacement description, markdown — keeps `## Acceptance Criteria` intact>
<<<END_ISSUE_UPDATE>>>
```

The orchestrator:

1. Posts an audit comment with the **previous body** in a `<details>`
   block (Linear has no body history).
2. Replaces the description.

Roll back manually by copy-pasting the audit `<details>` content back
into the issue body.

## Edge cases & gotchas

- **Re-dispatch fires once per new comment id.** A second read of the
  same comment is a no-op (loop avoidance via
  `state/last_user_comment.json`). Add another comment if you want to
  re-trigger.
- **First observation is silent only for stale backlog.** On the very
  first tick that inspects an `Awaiting approval` issue, redispatch
  fires *only if* the latest user comment is newer than the latest
  bot comment on the issue (i.e. it's a real reply to the bot's
  gating output). If the bot's output is the most recent activity,
  user comments are treated as historical noise and the comment id is
  seeded without firing — protecting against pre-rollout backlog
  re-architecting an approved decomposition. The
  `awaiting-human-reply` gate (Triage/Backlog) always fires on first
  observation since the gate itself is bot-set.
- **No re-trigger on `Awaiting approval` without a recognized label.**
  If an issue lands there outside the three label paths
  (`kind:final-pr` / `kind:decomposition` / `stuck`), it stays put and a
  warning is logged. Move it manually.
- **No re-trigger on Triage/Backlog without `awaiting-human-reply`.**
  A plain Triage/Backlog issue is already in the worker pickup pool —
  redispatch deliberately does not touch it (workers will see the new
  comment in the prompt on the next natural pickup).
- **Assigned issues are not yanked.** If a worker is mid-run on the
  issue, redispatch skips it. Wait for the run to finish, then comment.
- **Triage children gated by parent.** Comments on a Triage child whose
  parent isn't approved yet are read by Architect *only when the child
  is eventually picked up*. Comment on the parent if you need the
  decomposition reconsidered.
- **Description rewrites are full-replacement.** No diff/patch format.
  The agent must keep `## Acceptance Criteria` intact; if it doesn't,
  the AC parser falls back to an empty tuple on the next run.

## Killswitches

Each part is independently toggled in `config/orchestrator.yaml` under
`features:`:

```yaml
features:
  user_comments_in_prompt: true   # inject user comments into role prompts
  comment_redispatch: true        # re-trigger gated issues on new comment
  agent_body_edits: true          # honour <<<ISSUE_UPDATE>>> markers
```

Set any to `false` to disable without code changes.

## Latency

- Re-dispatch ticks every ~30s (housekeeping interval).
- Workers repoll on `poll_interval_seconds` (~20s default).
- Worst-case latency from comment to action is one tick.

## Quick patterns

- *"Change approach mid-flow"* → comment on the Backlog/Review issue;
  Coder/Reviewer reads it on next pickup.
- *"Answer an agent's clarifying question"* → reply on the issue (it
  will have an `awaiting-human-reply` label and a `BLOCKED: <question>`
  comment from the agent); within ~30s the label is stripped and the
  agent re-runs with your answer.
- *"Reject the architect's split"* → comment on the parent in Awaiting
  approval; children get archived, Architect re-runs.
- *"PR is wrong, redo it"* → comment on the issue in Awaiting approval
  (`kind:final-pr`); Reviewer re-checks within 30s.
- *"I want a clean slate"* → edit the issue description manually;
  orchestrator-owned writes do not race human edits.
