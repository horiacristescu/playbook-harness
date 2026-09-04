# Trust in agent work

Playbook does not make agent work correct by declaration. It creates places
where mistakes can become visible: a plan a reviewer reads before any code
exists, tests beside each change, a session a human can look into. Trust grows
from those checks.

## Start with the intended result

A technically clean implementation can still solve the wrong problem.

The task records the human's intent before the code changes, so a plan review
can ask whether the proposed work matches that intent and whether the planned
tests would prove the right thing. Writing the plan down makes assumptions easier to inspect before
they harden into code.

## Test the state that matters

Playbook encourages a test near each meaningful change.

The best test depends on the claim. A unit test may prove a function, an
integration test may prove that components work together, and a browser probe
or golden trace may be the only way to prove what a user or computer-use agent
can actually do.

Test the resulting state. Recording that an action was attempted proves less.

## Use fresh readers

Plan and implementation reviews give the work another reading.

A reviewer may notice an unsupported assumption, a missing test, or a simpler
design. It may also misunderstand the project, because it does not share all of
the human's context. A review is one more reading; the agent and the human
decide which of its findings to keep.

## Enforce what should not depend on memory

Some workflow rules are too important to leave to model discipline.

Hooks can block recognized code edits without an active task, prevent gates
from being skipped, and keep unfinished work from being presented as complete.
The current gate is handed back to the agent on every tool call so it does not
have to remember where it was.

These controls have a known boundary. They cover the operations and paths the
harness can observe, and they do not turn an agent process into a secure
operating system.

## Keep live work observable

Managed sessions let a human or monitor inspect an agent while it works, which
matters most when execution is long or several agents are active.

A silent pane is not proof of completion. A sent message is not proof that the
agent read it. The monitor distinguishes what was recorded, observed, sent,
acknowledged, and acted on.

## Contain risk separately

`pb-sandbox` limits where a contained agent can write. Managed tmux sessions
provide persistence and observation. These are different jobs, and neither
does the other's work: the sandbox does not prove the implementation is
correct, and tests and review do not contain a hostile process.

Trust comes from combining the checks that fit the risk.

## Preserve enough evidence

A trustworthy result should be explainable later.

The task records what was attempted, tests record what must stay true, and
reviews record a second reading. The human
can see where confidence came from and where uncertainty remains, which is
what justified trust looks like here: evidence proportional to the claim.
