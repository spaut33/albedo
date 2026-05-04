# GitHub bot identity

Albedo opens PRs and posts review comments through a GitHub MCP server
that authenticates with whatever `GITHUB_PERSONAL_ACCESS_TOKEN` is in
your `$ALBEDO_HOME/.env`. If that PAT belongs to a human, every PR
shows you as the author — and GitHub forbids reviewing or declining
your own PR (HTTP 422 on `event: APPROVE`).

This doc covers the **recommended setup**: a single dedicated bot
account whose PAT and git identity Albedo uses instead of yours.

## Phase 1 (shipped): single shared bot

End state — one extra GitHub account, one PAT, one git identity. All
workers (Coder, Reviewer, Architect) act as that bot. You can review
their PRs from your own GitHub account.

### One-time setup

1. **Create a bot GitHub account.** Use a `+` alias on your email
   (`yourname+albedo-bot@gmail.com` works on most providers, including
   Gmail). Verify the address, enable 2FA.

2. **Invite the bot as a collaborator** on each repo Albedo will touch,
   with **Write** access. Accept the invite from the bot's inbox.

3. **Generate a PAT for the bot.** Either form works:

   *Fine-grained (recommended)* — Settings → Developer settings →
   Personal access tokens → Fine-grained tokens. Scope it to the
   target repos with:

   - **Contents**: Read/Write — push branches.
   - **Pull requests**: Read/Write — open PRs, post comments and
     reviews.
   - **Metadata**: Read — required for any fine-grained PAT.
   - **Actions**: Read — read workflow runs / jobs / logs (used by
     the CI-redispatch flow in `github_client.list_workflow_runs`,
     `list_workflow_jobs`, `get_workflow_job_logs`).
   - **Workflows**: Read/Write — only if Coder will commit changes
     under `.github/workflows/`. Read-only access to CI logs does not
     need this.

   *Classic* — `repo` + `workflow` covers the same surface
   (`workflow` implies actions:read and the right to push workflow
   files).

4. **Edit `$ALBEDO_HOME/.env`:**

   ```
   GITHUB_PERSONAL_ACCESS_TOKEN=ghp_<bot's token>

   # Optional but recommended — without these, commits in worktrees
   # inherit your global git config (i.e. show as authored by you).
   GITHUB_BOT_NAME=Albedo Bot
   GITHUB_BOT_EMAIL=12345678+albedo-bot@users.noreply.github.com
   ```

   For `GITHUB_BOT_EMAIL`, use the no-reply form GitHub generates for
   the bot (Settings → Emails → "Keep my email address private"
   shows it as `<id>+<login>@users.noreply.github.com`). That keeps
   the bot's real address out of git history but still attributes
   commits to the bot's avatar.

5. **`albedo preflight`** — the GitHub check should now report
   `GitHub user=<bot-login> (commits as <name> <email>)`, not your
   own login.

6. **Run a smoke task.** On GitHub, the PR's author is the bot. In
   the worktree, `git log -1 --pretty='%an <%ae>'` shows the bot's
   identity. From your own GitHub account you can now press
   *Request changes* / *Approve* on the PR.

### What stays the same

The Reviewer role still posts `event: COMMENT` (not `APPROVE`),
because Coder and Reviewer share one GitHub identity — same 422 as
before, just with the bot as the offending self-reviewer instead of
you. The verdict still flows through Linear (`VERDICT: APPROVE` /
`REQUEST_CHANGES` parsed by the orchestrator → Linear state move).
That's unchanged from before this rollout. See `prompts/reviewer.md`.

## Phase 2 (deferred): N bots, one per agent

The single-bot setup gets you out of the self-review jam, but doesn't
restore *real* GitHub `event: APPROVE` events — for that you need
distinct identities for the agent that opens the PR and the agent
that reviews it. The wiring is already in place: `load_github_pat`
and `load_bot_identity` accept an `agent_id`, and try
`<NAME>_<AGENT_ID>` before falling back to the shared default. So the
remaining work is operational, not architectural.

When you're ready:

1. **Create N GitHub accounts** (one per agent), e.g.
   `agent-1-bot`, `agent-2-bot`, with the same `+`-alias email
   trick. Invite each as a Write collaborator on the target repos.

2. **Generate one PAT per account** with the same scopes as Phase 1.

3. **Add per-agent overrides to `.env`:**

   ```
   GITHUB_PERSONAL_ACCESS_TOKEN_1=ghp_agent_one
   GITHUB_PERSONAL_ACCESS_TOKEN_2=ghp_agent_two
   GITHUB_PERSONAL_ACCESS_TOKEN_3=ghp_agent_three
   GITHUB_BOT_NAME_1=Albedo Bot 1
   GITHUB_BOT_EMAIL_1=11111111+agent-1-bot@users.noreply.github.com
   # ... etc.
   ```

   The shared `GITHUB_PERSONAL_ACCESS_TOKEN` can stay as a fallback
   (e.g. for housekeeping, which has no agent context).

4. **Restore the self-PR filter.** With distinct identities an agent
   *can* approve PRs opened by other agents but still cannot approve
   its own. The candidate iteration in `worker.run_loop` should skip
   Review candidates whose `PR:` comment was authored by us.
   `find_pr_comment_author` and `_is_self_authored_pr` may already
   exist in earlier git history (they were dropped as no-ops when
   GitHub identity was shared).

5. **Switch the reviewer prompt back to event-typed reviews.** In
   `prompts/reviewer.md`, post `event: APPROVE` for APPROVE and
   `event: REQUEST_CHANGES` for REQUEST_CHANGES. The "always
   COMMENT" workaround can go away. Keep `VERDICT:` as the
   orchestrator's canonical signal — GitHub events are belt to its
   suspenders.

### Why we punted on N bots for the POC

- Inviting and signing into N GitHub accounts is meaningfully more
  setup work than N Linear users (each account needs its own login
  session, 2FA, email verification).
- The orchestrator already produces correct outcomes through the
  Linear state model. The only thing missing under one bot is the
  GitHub-side "Approved" badge — pretty, but not load-bearing.
- The implementation effort is well-defined and small (the items
  above) but the *operational* effort (creating bot accounts, paying
  for seats if the org plan caps collaborators) is non-trivial.
  Defer until someone actually needs the green check.

### Definition of done for Phase 2

- N PATs in `.env`, each scoped to its own bot account.
- Each `claude -p` child uses the right PAT (no shared default in
  worker mode).
- A test PR opened by Agent 1 is approved with `event: APPROVE` by
  Agent 2 — GitHub displays the green Approved badge — without the
  orchestrator hitting the 422 path.
- Self-PR skip filter prevents an agent from being assigned to
  review its own PR.
