# Trust in agent work

Playbook does not make agent work correct by declaration.

It creates places where mistakes can become visible and where evidence can be
kept. Trust grows from those checks.

## Start with the intended result

A technically clean implementation can still solve the wrong problem.

The task records the human's intent before the code changes. A plan review can
then ask whether the proposed work matches that intent, whether important
assumptions are still hidden, and whether the planned tests would prove the
right thing.

Writing the plan makes assumptions easier to inspect before they harden into
code.

## Test the state that matters

Playbook encourages a test near each meaningful change.

The best test depends on the claim. A unit test may prove a function. An
integration test may prove components work together. A browser probe or golden
trace may be needed to prove what a user or computer-use agent can actually do.

The important part is to test the resulting state, not merely record that an
action was attempted.

## Use fresh readers

Plan and implementation reviews give the work another reading.

A reviewer may notice an unsupported assumption, missing test, or simpler
design. It may also misunderstand the project because it does not share all of
the human's context.

Reviews are evidence and criticism, not authority. Their useful findings are
adopted. Their bad ideas are rejected.

## Enforce what should not depend on memory

Some workflow rules are too important to leave to model discipline.

Hooks can block recognized code edits without an active task, prevent gates
from being skipped, and keep unfinished work from being presented as complete.
The current gate is returned to the agent so it does not have to remember where
it was.

These controls have a known boundary. They cover the operations and paths the
harness can observe. They do not turn an agent process into a secure operating
system.

## Keep live work observable

Managed sessions let a human or monitor inspect an agent while it works.

Observation matters most when execution is long, external systems are involved,
or several agents are active. A silent pane is not proof of completion. A sent
message is not proof that the agent read it. The monitor distinguishes what was
recorded, observed, sent, acknowledged, and acted on.

This makes uncertainty explicit instead of hiding it behind a status label.

## Contain risk separately

`pb-sandbox` limits where a contained agent can write. Managed tmux sessions
provide persistence and observation. These are different jobs.

The sandbox does not prove the implementation is correct. Tests and review do
not contain a hostile process. Trust comes from combining the checks that fit
the risk.

## Preserve enough evidence

A trustworthy result should be explainable later.

The task records what was attempted. Tests record important expectations. Logs
and traces preserve live facts. Reviews record a second reading. The human can
see where confidence came from and where uncertainty remains.

That is justified trust: not certainty, but evidence proportional to the claim.
