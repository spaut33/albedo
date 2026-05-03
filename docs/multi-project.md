# Multi-project setup

Albedo serves any number of target repos. Each one carries its own
`.albedo.yaml` at its root; `albedo run` walks up from the current
directory to find it. To work with two repos, open two shells and run
`albedo` from each.

## Per-repo manifest

Drop a `.albedo.yaml` at the root of every target repo (use
`albedo init-repo` to generate the skeleton):

```yaml
# /home/roman/code/myrepo/.albedo.yaml
name: myrepo                # state and worktree dir slug
linear:
  project: My Repo          # exact Linear project name
repo:
  base_branch: main
  github:
    owner: me
    repo: myrepo
```

`name` is the stable slug for `~/.local/state/albedo/<name>` and
`~/.local/share/albedo/worktrees/<name>`. Renaming the directory does
not change it; only editing this field does.

`linear.project` is the exact display name of the Linear project that
holds issues for this repo. Albedo resolves it to a UUID at startup
via the `projects(filter:{name:{eq:…}})` query, and verifies the
project is attached to `linear.team`.

## Global config

`$ALBEDO_HOME/config.yaml` (default `~/.config/albedo/config.yaml`,
seeded by `albedo init`) carries cross-project knobs only:

```yaml
workers: 3
poll_interval_seconds: 20
linear:
  team: AI-Team             # Linear team key or exact name
usage:
  rolling_window_token_cap: 200000
models:
  coder: claude-opus-4-7
  reviewer: claude-sonnet-4-6
```

There is no `projects:` map — per-repo settings live with each repo.
`linear.team` is shared across all repos albedo touches.

## State and worktree scoping

State and worktrees are namespaced by `name`:

```
~/.local/state/albedo/<name>/        # logs, usage.db, heartbeats, claims
~/.local/share/albedo/worktrees/<name>/
```

Two `albedo run` invocations against different repos never collide. Pin
custom locations via `state_dir` and `worktree_root` in the global
config; the per-project `<name>/` suffix is always appended.

## Running

```bash
cd ~/code/myrepo
albedo run                       # walks up, finds .albedo.yaml, starts workers
albedo --once --issue MYREPO-42  # one CODER iteration on a specific issue
```

If `.albedo.yaml` is missing, the CLI exits with `error: No .albedo.yaml
found in <cwd> or any parent.`

## What gets filtered by project

All Linear queries that drive the polling loop and housekeeping include
a `project: {id: {eq: <project_id>}}` filter:

| Caller | Query | Behaviour |
|---|---|---|
| `worker.run_loop` | `list_pickup_issues` | Only unclaimed issues in the active project |
| `worker.run_loop` (recovery) | `list_assigned_issues` | Stale-claim recovery scoped to the active project |
| `housekeeping.release_decomposed_children` | parents-with-`kind:decomposition`-in-Done | Only this project |
| `housekeeping.sync_merged_prs_to_done` | `Awaiting approval` + `kind:final-pr` | Only this project |
| `housekeeping.archive_old_done_issues` | completed older than N days | Only this project |
| `comment_redispatch.redispatch_on_new_user_comments` | human-gated states | Only this project |

ARCHITECT child creation reads the parent's `project.id` and passes it
to `linear.create_issue(projectId=…)` so a decomposition keeps its
children inside the same project.

## Operational checklist

### One-time per Linear team
1. `albedo init` — seed `$ALBEDO_HOME` with config + prompts.
2. Edit `~/.config/albedo/config.yaml` (`linear.team`) and `.env`
   (`LINEAR_API_KEY`, `GITHUB_PERSONAL_ACCESS_TOKEN`).
3. From any target repo, `albedo setup` — bootstraps the team's
   workflow states and labels and verifies the repo's Linear project.

### Adding a new repo
1. Create the project in Linear UI — pick the team, give it the exact
   name you intend to use in the manifest.
2. `cd ~/code/newrepo && albedo init-repo`, then edit `name`,
   `linear.project`, and `repo.github.*`.
3. `albedo setup` from inside the repo — confirms the new project
   resolves.
4. `albedo run` from inside the repo.

### Migrating existing issues
The polling filter ignores issues that have no project attached, even
when team, state, and assignee match. To make existing issues pickable:
- Open the issue in Linear → set the **Project** field to the matching
  project, **or**
- Bulk-select issues → "Move to project" → pick the project.

Done/archived issues don't need to be migrated; the filter only matters
for active states (Triage, Backlog, Review, Awaiting approval, In
Progress).

## Limitations

- One process per repo. Running two repos in parallel needs two
  `albedo run` invocations, each from inside its own target repo.
- Shared agent users across repos. Linear comment authorship doesn't
  distinguish which repo a worker was running.
- `linear.team` is shared. To serve a project in a different team
  you'd need a separate `$ALBEDO_HOME` (set the env var) and a
  separate `albedo setup`.
- Project lookup is by exact name. Renaming a project in Linear breaks
  the mapping until the manifest catches up.
