# Command reference

This page is for lookup. The [getting started guide](getting-started.md)
explains the normal first-run sequence.

## Project orientation

```bash
pb-tasks bootstrap
```

Print the project mind map, recent context, pending tasks, and task command
reference.

```bash
pb-tasks status
pb-tasks list
pb-tasks list --pending
```

Show the active gate position or list project tasks.

## Task lifecycle

```bash
pb-tasks new <type> <name> [intent]
pb-tasks new --stub <type> <name> [intent]
```

Create a task. A stub records the intent now and expands when work begins.

Current built-in task types are `audit`, `bugfix`, `build`, `cleanup`,
`eval`, `feature`, `monitor`, `ops`, `quick`, `refactor`, and `research`.
Projects may also define custom types.

`monitor` creates a normal owned task whose body is a long-lived blackboard.
Its lane gates may close in event order while other task types retain strict
first-open-gate progression.

```bash
pb-tasks work <N>
pb-tasks work done
pb-tasks freehand
```

Activate one task, finish the active task after all gates close, or enter
user-directed freehand mode.

```bash
pb-tasks context <N>
pb-tasks log
pb-tasks narrative --status
```

Read chat context attributed to one task, print the compact human-message log,
or inspect the longer work arcs recorded in the narrative.

## Reviews and retrospectives

```bash
pb-tasks plan-review <N>
pb-tasks impl-review <N>
```

Run a blind plan or implementation review.

```bash
pb-tasks panel-review [<N>]
pb-tasks panel-review --prompt "..."
pb-tasks panel-review --bare --prompt "..."
```

Run a multi-model panel. A task is optional when a standalone prompt is given.
`--bare` removes project context.

```bash
pb-tasks retro [--since N]
pb-tasks intent <N>
```

Start a project retrospective. The retro reads across work and looks for
lessons worth installing in the system. `intent` compares the task's stated
intent with chat, code, and tests.

```bash
pb-tasks global-retro-collect --since <DATE> <ROOT> [ROOT...]
```

Collect Playbook evidence from one or more project roots into a portable
archive for a cross-project retrospective.

## Project and runtime diagnostics

```bash
pb-tasks doctor
pb-tasks runtime-audit
pb-tasks runtime-info
```

`doctor` checks the current project integration. `runtime-audit` verifies
installed artifacts. `runtime-info` prints the authoritative runtime schema
and Git commit.

## Branch preparation

```bash
pb-tasks prepare-merge [--target <branch>] [--dry-run]
```

Prepare Playbook task and chat identifiers for a clean branch merge. Use
`--dry-run` to inspect the proposed changes first.

## Managed sessions

```bash
pb-session
pb-session list
pb-session status <name-or-native-id>
```

Start the default provider in managed tmux, list recorded sessions, or inspect
one session and its resume route.

Provider launchers are also available:

```bash
pb-claude [provider arguments]
pb-codex [provider arguments]
pb-agy [provider arguments]
pb-pi [provider arguments]
```

They delegate provider arguments unchanged while supplying the Playbook launch
environment required by that provider.

## Tmux transport

```bash
pb-tmux-agent start reviewer codex
pb-tmux-agent send reviewer "review this change"
pb-tmux-agent peek reviewer 50
pb-tmux-agent attach reviewer
pb-tmux-agent detach reviewer
pb-tmux-agent tail reviewer 50
pb-tmux-agent wait reviewer --timeout 30
pb-tmux-agent stop reviewer
```

Use `--namespace` to isolate campaigns and `--json` for controllers. Run
`pb-tmux-agent --help` for provider, working-directory, environment, and model
options.

If tmux is absent, session commands stop before creating state. Install tmux
with the machine's package manager and retry. `pb-tasks init` never installs
machine packages.

## Contained agents

```bash
pb-sandbox --prompt "..." --agent codex
```

Run a contained headless agent. Use `pb-sandbox --help` for provider,
filesystem, model, and streaming options.

## Historical evaluation

```bash
pb-arena case list
pb-arena case doctor <case>
pb-arena canary list
pb-arena campaign run <campaign>
```

Arena reconstructs Git-pinned historical project moments without touching live
worktrees. A case doctor reconstructs and validates a case; it does not run an
agent. A frozen campaign is the separate operation that executes declared
commands and retains evidence.

See the [Arena guide](../arena/README.md) before running a campaign with a real
provider.

## Project initialization

```bash
pb-tasks init
pb-tasks init /path/to/project
pb-tasks init --provider codex
pb-tasks init --no-hooks
```

Initialize all detected providers, one exact project, one provider, or refresh
assets while preserving current hook files.

See [installation and upgrades](install-and-upgrade.md) for lifecycle and
migration commands.
