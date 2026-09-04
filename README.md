# Playbook Harness

Playbook is a project harness for coding agents.

It keeps plans, progress, and project knowledge in ordinary files that remain
available when a conversation ends or another agent takes over. Work is
organized in `task.md`, where the agent follows one step at a time and tests
the result before moving on.

You can still talk to the agent normally. Playbook logs your messages to
`.agent/chat_log.md` and turns the work into files, so the conversation leaves
something behind.

> Playbook uses ordinary agent-extension mechanisms to make work unusually durable and inspectable.

## Why it exists

A coding agent can do useful work in one session. A project lasts longer.

Over time, decisions get buried in chat and tests gain meanings that are not
obvious from their code. The human ends up being the one who remembers why the
project looks the way it does.

Playbook writes those things down inside the project: your messages go to a
chat log, each piece of work gets a `task.md`, and the architecture and
standing decisions live in `MIND_MAP.md`. A future agent picks them up
where the last one stopped, and you can inspect or edit them at any time.

## What working with it feels like

You describe the work in chat. The agent turns it into a task with an explicit
intent and a sequence of gates.

The task can be reviewed before expensive work begins. During implementation
the current gate stays visible and tests are placed near the work they
protect, and when the task is done its file remains as a record of the
implementation.

For longer work, a managed session can keep the agent observable and
resumable. A monitor can watch several sessions without taking ownership away
from them. A retrospective can later turn a repeated lesson into a test or a
tool.

The harness supports Claude Code, Codex, OMP, Pi, and guidance-only use with
Antigravity. The task files, chat log, and mind map are the same whichever
agent is running.

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
