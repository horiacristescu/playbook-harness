# Getting started

Playbook has two installation steps.

The machine runtime is installed once. Each project is then initialized
separately.

## Install the runtime

Playbook requires macOS or Linux, Bash, Git, Python 3.10 or newer, and at least
one supported agent CLI.

```bash
curl -fsSL https://raw.githubusercontent.com/horiacristescu/playbook-harness/main/install.sh | bash
```

Make sure `${XDG_BIN_HOME:-$HOME/.local/bin}` is on your `PATH`.

The installer does not install agent CLIs or log in to their services. Keep
using the provider's normal login process.

## Initialize one project

Move to the project root and run:

```bash
cd /path/to/project
pb-tasks init
```

Initialization detects supported agents already installed on the machine. It
adds their project-local guidance and hooks, creates the shared `.agent/`
state, and creates `MIND_MAP.md` when it is missing.

It does not scan parent directories or initialize other projects.

Existing guidance is preserved. If Playbook cannot merge a change safely, it
writes a proposal under `.agent/templates/` and asks for a manual merge.

## Start the agent

Launch the supported agent from the project root in the usual way.

Playbook's project guidance asks the agent to bootstrap at the start of the
session:

```bash
pb-tasks bootstrap
```

Bootstrap shows the project map, recent context, pending tasks, and the commands
needed to continue. It is orientation, not permission to choose work. The agent
returns to chat and waits for direction.

## Create the first task

Tell the agent what you want to build or fix. For repository code work, it will
create a task:

```bash
pb-tasks new feature export-report "Add CSV export to the report page"
```

The command creates a numbered task folder containing `task.md`. The agent
fills in the intent and work plan before implementation.

The command prints the new task number. Start the task with that number:

```bash
pb-tasks work <N>
```

From there, the agent works through the gates in order. It closes each gate
with a short outcome and keeps tests close to the change they verify.

When every gate is complete:

```bash
pb-tasks work done
```

Markdown documentation, planning, and read-only investigation do not require a
task. Code changes do.

## Add a review

A plan can be reviewed before implementation:

```bash
pb-tasks plan-review <N>
```

The implementation can be reviewed before the task is closed:

```bash
pb-tasks impl-review <N>
```

Reviews are advisory. Read their findings and keep only what fits the project's
intent and evidence.

## Use a managed session

For longer work, start the default provider inside a managed tmux session:

```bash
pb-session
```

The session can be named, inspected, steered, and found again after an
interruption. Direct provider launches still work and use the same project
state.

See [sessions and monitoring](sessions-and-monitoring.md) when you want several
agents or a monitor.

## Check the installation

Run project diagnostics from the project root:

```bash
pb-tasks doctor
```

Check the installed runtime from any directory:

```bash
pb-tasks runtime-audit
```

For upgrades, provider-specific limits, and migration from the former Claude
Marketplace release, see [installation and upgrades](install-and-upgrade.md).
