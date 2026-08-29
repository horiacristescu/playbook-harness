# How Playbook works

Playbook follows the life of a piece of work from conversation to durable
project state.

The normal interaction still begins in chat.

## The human describes the work

The first description can be incomplete. The human may know the problem but
not the implementation. The agent can inspect the project, ask questions, and
help turn the request into a bounded task.

The original conversation remains useful because it contains emphasis,
corrections, and changes of mind that a clean specification may hide.

## The work gets a task

Repository code work is placed in a `task.md` file.

The task states the intent and defines a sequence of gates. A gate is one unit
of work that can be completed and checked before the next one begins. New gates
can be added when the work reveals something the original plan missed.

The task is a living work program, not a contract frozen at the start.

A plan review can inspect it before implementation. The reviewer reads the
task and project state without relying on the conversation that produced the
plan. This gives the project a fresh reading while changes are still cheap.

## The agent works one gate at a time

When a task is active, Playbook keeps its next open gate in view.

The agent does the work, checks the result, and closes that gate with a short
outcome. Hooks prevent several common mistakes, such as editing recognized code
without an active task or skipping ahead in the gate sequence.

The gates can change shape with the work. A known fix may need only a few. A
research task may use rounds of hypothesis, evidence, and conclusion. A larger
implementation can alternate code changes with focused tests and checkpoints.

## Tests stay close to the decisions

Playbook treats tests as part of implementation, not as cleanup at the end.

A test records something the project expects to remain true. Placing it near
the change makes the evidence available while the decision is still fresh.
Some work needs code tests. Other work needs a live run, a browser probe, a
golden trace, a rubric, or direct human inspection.

The form changes. The purpose does not: make the result easier to trust without
asking the human to repeat the whole investigation.

## Review reads the result again

An implementation review looks for missed intent, weak evidence, unnecessary
complexity, and adjacent damage.

The reviewer is advisory. It can find a real omission or propose something
that does not fit the project. The agent and human keep the responsibility for
judging its suggestions.

## The task becomes history

When every gate is complete, the task is closed.

Its `task.md` remains in the project. It now explains more than the final diff:
what was intended, what path was taken, what changed during the work, and what
evidence supported the result.

Knowledge that matters beyond one task can be moved into the project mind map,
a test, a tool, a log, or agent guidance. The rest can remain in the task where
it is available without burdening every future session.

## The project continues

The next agent bootstraps from the current project rather than from a blank
conversation. It sees the project map, pending tasks, recent context, and the
workflow it must follow.

For longer or parallel work, managed sessions add names, observation, steering,
and recovery. A monitor can keep the project view while individual agents stay
focused on their own tasks.

Later, a retrospective can read across the whole arc. Its job is to find useful
experience that has not yet changed the system.
