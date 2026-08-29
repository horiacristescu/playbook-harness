# Why Playbook is built this way

Playbook starts from a small observation: a conversation is not a project.

A conversation can be productive. The agent may understand the problem, make
good decisions, and finish useful work, and then the conversation ends while
the project keeps going.

If the important state stays in chat, the human becomes responsible for
carrying it forward, and the next session begins with another explanation. Old
decisions are easy to repeat and hard to question because their reasons are no
longer visible anywhere.

Playbook writes it into the project instead. A hook appends each user message
to `.agent/chat_log.md`, every piece of code work gets a `task.md` holding its
plan and progress, and the architecture and standing decisions live in
`MIND_MAP.md`.

## Work should be an object

The central object in Playbook is `task.md`.

It begins with the human's intent, grows into a plan, and then becomes the
place where the agent records progress, tests, discoveries, and changes of
direction. When the work is finished, the same file remains as an account of
how the implementation actually went.

This is different from a disposable todo list. Another agent can read the
task, a judge can review it, the human can edit it while the work is underway,
and a later retrospective can compare the plan with what happened.

## Autonomy needs support

Coding agents are already capable of completing many clear tasks. Longer work
fails for different reasons: the agent loses the current intent, skips a
check, trusts an assumption that was never tested, or finishes something that
looks plausible but is difficult for the human to verify.

Playbook does not answer this with more reminders in a prompt. A hook echoes
the current gate after every tool call, blocks code edits when no task is
active, and refuses to close a task with open gates. Tests sit beside each
change, `pb-tasks plan-review` and `impl-review` bring in a reader who has not
seen the conversation, and `pb-session` puts long work in a tmux session a
human can look into.

These constraints are what let the human grant more autonomy.

## Use the agent's natural tools

Playbook is mostly text files and ordinary shell work.

Agents are well trained to read and edit text, and humans can do the same, so
there is no private planning database that only the harness understands. A
task can be opened in an editor, changed in conversation, passed to another
agent, or reviewed in a retro months later.

The command line is kept for operations that benefit from one reliable action:
starting a task, selecting the active task, running a review, or opening a
managed session. The human should not need to memorize the whole harness,
because each stage makes the next action clear.

## The human remains part of the system

Playbook is not designed to remove the human from the project.

The human supplies intent, notices when the project feels wrong, adds
constraints, and decides what is worth doing. The harness spares the human from
explaining the project again each session and makes the agent's work easy to
inspect, but it does not turn judgment into a background service.

This is why plans, tasks, tests, and monitor output remain readable: the human
can enter the work at any point without first reconstructing an invisible
agent state.

## The harness should learn

A project produces lessons as it runs: a bug that took a day to reproduce, a
deploy step everyone forgets, an API response that quietly changed shape. Some
of these were expensive to learn, some can only be seen while the failure is
live, and some will never matter again.

A retrospective asks which is which, and where each lesson should go. A
recurring invariant may become a test; a procedure the agent keeps repeating
may become a tool. The [memory page](memory.md) lists the other destinations.

The point is for those lessons to end up in a test, a tool, or the mind map,
where the next agent will meet them, rather than in another summary.

## Claims should remain honest

Playbook was developed through use across many real projects. That history is
useful, but it does not make every design choice universally correct.

The harness should state what it does, keep the task and chat record behind
important decisions, and leave room for revision. A result from one evaluation is a clue,
not a law.
