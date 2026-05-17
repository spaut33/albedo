# Changelog

## Unreleased

### CLI

- `albedo help` and `albedo help <subcommand>` are now valid entry
  points that mirror `albedo --help` and `albedo <subcommand> --help`,
  so usage is discoverable without remembering the `--help` flag.

### Setup-flow simplification

`albedo run`, `albedo preflight`, and `albedo setup` now auto-seed
their prerequisites instead of failing if they're missing:

- If `$ALBEDO_HOME/config.yaml` is missing, the templates are written
  and the command exits with a hint pointing at the placeholders to
  edit (`linear.team`, `LINEAR_API_KEY`, `GITHUB_PERSONAL_ACCESS_TOKEN`).
- If no `.albedo.yaml` is reachable upward from CWD, a skeleton is
  written in CWD with `repo.github.{owner,repo}` auto-filled from
  `git remote get-url origin` (when origin points at GitHub).
- `albedo run` invokes Linear bootstrap (states + labels + project
  verification) on first launch and writes a marker at
  `~/.local/state/albedo/<name>/.bootstrapped` so subsequent runs
  skip it.

`init`, `init-repo`, `preflight`, and `setup` remain explicit
subcommands for re-seeding (`init --force`), debugging, or running the
bootstrap by itself. None are required for a fresh setup — the
shortest happy path is now `cd <repo> && albedo run` × 3 (seed,
edit, repeat).

### Placeholder validation

Every required field that ships as `REPLACE_ME` in the seed templates
(`linear.team`, manifest `name`, `linear.project`,
`repo.github.owner`, `repo.github.repo`) now fails loudly at config
load with a pointer to the file that needs editing.

### Renamed to Albedo

The Python package is now `albedo` (was `orchestrator`); the PyPI
distribution is `albedo` (was `ai-orchestrator`); the CLI binary is
`albedo` (was `python -m orchestrator`). The `ORCHESTRATOR_NO_TUI`
environment variable is now `ALBEDO_NO_TUI`.

### Config layout: `$ALBEDO_HOME` + per-repo manifest

Project setup no longer ties albedo to a specific working directory.
Run `albedo` from inside any target repo and it walks up to find the
manifest — same as `git`/`cargo`/`uv`.

**New layout:**

```
$ALBEDO_HOME/                  # default: ~/.config/albedo
  config.yaml                  # global knobs (workers, polling, linear.team, …)
  .env                         # LINEAR_API_KEY, GITHUB_PERSONAL_ACCESS_TOKEN
  prompts/                     # operator-editable role templates
  mcp-servers.json             # passed to `claude -p --mcp-config`

~/.local/state/albedo/<name>/  # per-repo state: logs, usage.db, heartbeats
~/.local/share/albedo/worktrees/<name>/  # per-repo worktrees

<target-repo>/
  .albedo.yaml                 # name, linear.project, repo.{base_branch,github}
```

`<name>` comes from the manifest's `name` field. Two `albedo run`
invocations against different repos never collide.

**Removed:**

- `projects:` map in the global config.
- `--project <name>` CLI flag and `ORCHESTRATOR_PROJECT` env var.
- The expectation that albedo runs from its own source tree.
- `select_project`, `ProjectSelectionError`, `ProjectConfig`,
  `ProjectLinearConfig` from `albedo.config`.

**Added:**

- `albedo init` — seeds `$ALBEDO_HOME` from bundled defaults.
- `albedo init-repo` — writes a `.albedo.yaml` skeleton in CWD.
- `albedo.paths` — XDG resolvers for every default path.
- `albedo.repo_config` — `RepoManifest` schema and `load_repo_manifest`
  (walk-up discovery).
- `RepoManifestNotFoundError` — raised with an actionable message when
  no manifest is found between CWD and the filesystem root.

## Migration

This is a single-replacement change with no compatibility shims. Move
the existing tree to the new locations by hand, in this order:

```bash
# 0. Install the new entry point (one-time)
uv tool install /home/roman/dev/my_projects/ai_orchestrator

# 1. Seed $ALBEDO_HOME from bundled defaults
albedo init

# 2. Move the existing global config and secrets into $ALBEDO_HOME
mv ~/dev/my_projects/ai_orchestrator/.env ~/.config/albedo/.env
# Open ~/.config/albedo/config.yaml — copy linear.team, workers,
# poll_interval_seconds, usage.*, models.*, features.*, attachments.*,
# max_attempts_before_escalation from your old config/orchestrator.yaml.
# Drop the old `projects:` map, `state_dir`, and `worktree_root` (XDG
# defaults will take over). Drop the old config file when done.

# 3. For each project that lived under `projects:` in the old YAML,
#    write a .albedo.yaml at the target repo's root:
cd ~/dev/my_projects/ai_orchestrator/examples/sample-repo
$EDITOR .albedo.yaml          # already provisioned in this checkout

cd ~/code/myrepo
albedo init-repo
$EDITOR .albedo.yaml          # set name, linear.project, repo.github.{owner,repo}

# 4. Move existing per-project state and worktrees to XDG locations.
#    `<name>` matches the `name:` field in the corresponding .albedo.yaml.
mkdir -p ~/.local/state/albedo ~/.local/share/albedo/worktrees
mv ~/dev/my_projects/ai_orchestrator/state/sample \
   ~/.local/state/albedo/sample
mv ~/dev/my_projects/ai_orchestrator/.worktrees/sample \
   ~/.local/share/albedo/worktrees/sample

# 5. Verify
cd ~/code/myrepo            # or any registered target repo
albedo preflight            # exit 0 = green
albedo run                  # workers spawn against the discovered .albedo.yaml
```

If the old `state/` or `.worktrees/` directories were never load-bearing
(POC stage, no in-flight work), skip step 4 and let the new locations
start fresh.

## Notes

- `LINEAR_API_KEY` and `GITHUB_PERSONAL_ACCESS_TOKEN` continue to be
  read from process env first, then from the `.env` file — only the
  `.env` location moved (now `$ALBEDO_HOME/.env`).
- `--config`, `--prompts-dir`, and `--mcp-config` flags still exist for
  power users who want to point at custom locations; the defaults are
  the XDG paths.
- The old `python -m orchestrator` entry point is gone. `python -m
  albedo` works identically to `albedo` for users who prefer it.
