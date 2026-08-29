# Why Playbook is built this way

Playbook starts from a small observation: a conversation is not a project.

A conversation can be productive. The agent may understand the problem, make
good decisions, and finish useful work. But the conversation is temporary.
The project keeps going.

If the important state stays in chat, the human becomes responsible for
carrying it forward. The next session begins with another explanation. Old
decisions are easy to repeat and hard to question because their reasons are no
longer visible.

Playbook puts the state in the project instead.

## Work should be an object

The central object in Playbook is `task.md`.

It begins with the human's intent. It grows into a plan, then becomes the place
where the agent records progress, tests, discoveries, and changes of direction.
When the work is finished, the same file remains as a useful account of the
implementation.

This is different from a disposable todo list. The task can be read by another
agent. A judge can review it. The human can edit it while the work is underway.
A later retrospective can compare its plan with what actually happened.

The work has become something the project can inspect.

## Autonomy needs support

Coding agents are already capable of completing many clear tasks. Longer work
fails for different reasons. The agent loses the current intent. It skips a
check. It trusts an assumption that was never tested. It finishes something
that looks plausible but is difficult for the human to verify.

Playbook does not answer this with more reminders in a prompt.

The harness keeps the current gate visible. Hooks enforce a few important
boundaries. Tests provide evidence. Reviews bring in a second reading. Session
tools make live work observable and steerable.

These constraints are not opposed to autonomy. They are what allow the human
to grant more of it.

## Use the agent's natural tools

Playbook is mostly text files and ordinary shell work.

Agents are well trained to read and edit text. Humans can do the same. There is
no private planning database that only the harness understands. A task can be
opened in an editor, changed in conversation, passed to another agent, or
studied months later.

The command line is kept for operations that benefit from one reliable action:
starting a task, selecting the active task, running a review, or opening a
managed session.

The human should not need to memorize the whole harness. Each stage should make
the next action clear.

## The human remains part of the system

Playbook is not designed to remove the human from the project.

The human supplies intent, notices when the project feels wrong, adds
constraints, and decides what is worth doing. The harness reduces repeated
explanation and makes the agent's work easier to inspect. It does not turn
judgment into a background service.

This is why plans, tasks, tests, and monitor output remain readable. The human
can enter the work at any point without reconstructing an invisible agent
state first.

## The harness should learn

A project produces experience as it runs. Some of that experience is expensive
to obtain. Some evidence disappears after the live failure is gone. Some
lessons matter only once.

A retrospective asks which is which.

A recurring invariant may become a test. A repeated procedure may become a
tool. A live failure condition may become a monitor rule. Evidence that will
disappear may deserve a log. A correction to the project model may need to be
explained to the human.

The aim is not to produce another summary. It is to let experience improve the
environment in which future work happens.

## Claims should remain honest

Playbook was developed through use across many real projects. That history is
useful, but it does not make every design choice universally correct.

The harness should state what it does, preserve the evidence behind important
decisions, and leave room for revision. A result from one evaluation is a clue,
not a law.

The project can improve because its working ideas are visible enough to test.
