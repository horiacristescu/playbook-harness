# Playbook Harness

Playbook is a project harness for coding agents.

It keeps plans, progress, tests, and project knowledge in ordinary files. These
files remain available when a conversation ends or another agent takes over.

Work is organized in `task.md`. The agent follows one step at a time, records
what it learns, and tests the result before moving on.

You can still talk to the agent normally. Playbook gives that conversation
somewhere durable to go.

## Why it exists

A coding agent can do useful work in one session. A project lasts longer.

Over time, decisions become buried in chat. Plans change. Tests gain meanings
that are not obvious from their code. The human ends up remembering why the
project looks the way it does.

Playbook moves that state into the project. The current agent can read it. A
future agent can pick it up. The human can inspect and change it at any time.

## What working with it feels like

You describe the work in chat. The agent turns it into a task with an explicit
intent and a sequence of gates.

The task can be reviewed before expensive work begins. During implementation,
the current gate stays visible. Tests are placed near the work they protect.
When the task is done, its file remains as a record of the implementation.

For longer work, a managed session can keep the agent observable and
resumable. A monitor can watch several sessions without taking ownership away
from them. A retrospective can later turn repeated lessons into better tests,
tools, logs, guidance, or project memory.

The harness supports Claude Code, Codex, OMP, Pi, and guidance-only use with
Antigravity. The project state is shared even when the agent changes.

## Install

Playbook runs on macOS and Linux. It requires Bash, Git, Python 3.10 or newer,
and at least one supported agent CLI.

```bash
curl -fsSL https://raw.githubusercontent.com/horiacristescu/playbook-harness/main/install.sh | bash
cd /path/to/project
pb-tasks init
```

The installer does not install or log in to agent providers. Project
initialization changes only the project you name, or the current directory when
no path is given.

Start a session by asking the agent to bootstrap, or run:

```bash
pb-tasks bootstrap
```

The [getting started guide](docs/getting-started.md) walks through the first
task.

## Read more

- [Why Playbook is built this way](docs/philosophy.md)
- [How a project moves through Playbook](docs/how-playbook-works.md)
- [How project memory works](docs/memory.md)
- [What the harness can and cannot make trustworthy](docs/trust.md)
- [Sessions and monitoring](docs/sessions-and-monitoring.md)
- [Installation, upgrades, and migration](docs/install-and-upgrade.md)
- [Command reference](docs/command-reference.md)
- [Release notes](docs/RELEASE_NOTES.md)

The [Arena guide](arena/README.md) covers historical evaluation campaigns. The
[OMP guide](docs/integrating-omp.md) describes that provider's current
integration boundary.
