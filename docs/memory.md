# Project memory

Playbook keeps memory in the project because no single agent session lasts as
long as the work.

It does not put everything into one large context. Different kinds of memory
have different jobs.

## Conversation keeps the human voice

The chat log preserves what the human asked, corrected, rejected, and cared
about while the project developed.

This matters because intent is rarely written once. It becomes clearer through
examples and through moments when the human says that something is wrong.

The chat log is evidence. It is not a finished project description.

## Tasks remember work

Each `task.md` remembers one bounded piece of work.

Before implementation it contains intent and a plan. During implementation it
holds the current position and the discoveries that changed the plan. Afterward
it remains as the implementation workbook.

Tasks make it possible to study work later. The project can compare what was
intended with what was built and with what later tasks revealed.

## The mind map remembers the project

`MIND_MAP.md` is the project's recurrent state.

It carries architecture, important decisions, active systems, and routes to
deeper evidence. It should help a fresh agent orient quickly. It should not
become a copy of every task or conversation.

When the map and the project disagree, the map must change.

## Tests remember expectations

A test is memory with an executable check attached.

It preserves an invariant that would otherwise need to be rediscovered. The
test is especially valuable when the original failure was difficult to
reproduce or when a plausible implementation could silently break the same
behavior later.

Tests do not replace the reason behind an expectation. When that reason is not
obvious, the task or nearby documentation should keep it.

## Logs remember live evidence

Some facts exist only while a system is running.

An API response changes. A long pipeline fails after an hour. A browser reaches
a state that cannot be reconstructed from source code alone. Useful logs and
captured traces make later debugging possible.

Keeping every event forever is not the goal. The project should preserve the
evidence that would be expensive or impossible to obtain again.

## Sessions remember orientation

A managed session records enough identity to answer a practical question:
which agent was working on what, and how can that conversation be found again?

The session record is not a second transcript. Provider history keeps the
conversation. Playbook keeps the handles needed to return to it.

## Retros decide what moves forward

Most details should stay where they were produced. A small part deserves a more
durable destination.

The useful question is:

> Is this worth transmitting?

Something may be worth transmitting because it will make future debugging
possible, lower the cost of future work, or increase justified trust.

The destination depends on the lesson. An invariant belongs in a test. A
procedure may belong in a tool. A disappearing condition may belong in a log.
A correction to the project model may belong in the mind map. Something the
human repeatedly misunderstands may need a plain explanation.

Memory is useful when it changes future work. More stored text is not, by
itself, better memory.
