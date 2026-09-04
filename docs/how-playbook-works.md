# How Playbook works

Playbook follows a piece of work from a chat message to files in the
repository. The normal interaction still begins in chat.

## The human describes the work

The first description can be incomplete. The human may know the problem but
not the implementation, and the agent can inspect the project and ask
questions until the request becomes a bounded task.

The original conversation remains useful because it contains emphasis,
corrections, and changes of mind that a clean specification may hide.

## The work gets a task

Repository code work is placed in a `task.md` file.

The task states the intent and defines a sequence of gates, where a gate is
one unit of work that can be completed and checked before the next one begins.
New gates can be added when the work reveals something the original plan
missed.

`pb-tasks plan-review` can inspect the task before implementation. The reviewer reads
the task and project state without relying on the conversation that produced
the plan, which gives the project a fresh reading while changes are still
cheap.

## The agent works one gate at a time

When a task is active, a hook prints its next open gate after every tool
call.

The agent does the work, checks the result, and closes that gate with a short
outcome. Hooks prevent several common mistakes, such as editing recognized code
without an active task or skipping ahead in the gate sequence.

The gates can change shape with the work. A known fix may need only a few,
while a larger implementation alternates code changes with tests and
checkpoints.

## Tests stay close to the decisions

In Playbook, tests are written while the work is being done.

A test records something the project expects to remain true, and placing it
near the change makes the evidence available while the decision is still
fresh. Some work needs code tests; other work needs a live run, a browser
probe, or a human looking at the result. Whatever the form,
the purpose is the same: make the result easier to trust without asking the
human to repeat the whole investigation.

## Review reads the result again

An implementation review looks for missed intent, weak evidence, and damage
to nearby code.

The reviewer is advisory. It can find a real omission or propose something
that does not fit the project, and the agent and human keep the responsibility
for judging its suggestions.

## The task becomes history

When every gate is complete, the task is closed. Its `task.md` remains in the
project, and it now explains more than the final diff: what was intended, what
changed along the way, and what evidence supported the result.

Knowledge that matters beyond one task can be moved into the mind map, a
test, or a tool. The rest can stay in the task, where
it is available without burdening every future session.

## The project continues

The next agent starts with `pb-tasks bootstrap` rather than a blank
conversation. It prints `MIND_MAP.md`, the pending tasks, and the last chat
messages.

For longer or parallel work, `pb-session` gives each agent a name and a tmux
pane you can peek at or send a message to.
A monitor agent can watch several such sessions while each stays focused on
its own task. Later, `pb-tasks retro` reads across the closed tasks and the
chat log for lessons that have not yet become a test, a tool, or a mind map
entry.
