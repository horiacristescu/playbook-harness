---
name: monitor
description: Supervise several managed Playbook sessions from one ordinary session. Use when the user asks to watch, coordinate, inspect, or steer a tmux-hosted agent fleet through pb-session. Do not use for a single agent, automatic scheduling, transcript recovery, or background surveillance.
---

# Fleet Monitor

Operate as a project-level observer and coordinator. Use only public
`pb-session` and `pb-tasks` facts. Do not create a second state hierarchy,
scrape provider logs, infer completion from silence, or treat tmux as a sandbox.

## Establish authority

1. Read the user's monitoring request and any operator-supplied Markdown policy.
2. State which decisions the user delegated. Keep task content, destructive
   actions, spending, scope changes, and ambiguous ownership with the user unless
   they explicitly delegated them.
3. Never edit worker-owned implementation files. Monitoring is read-only except
   for authorized messages, task coordination, and requested reporting or
   cultural distillation.
4. Report your own mistaken interruption, routing, or inference explicitly.

## Build the fleet view

Run:

```bash
pb-session list --json
pb-tasks status
```

For each relevant managed session, run:

```bash
pb-session status NAME --json
pb-session peek NAME 40
```

Relate session name and native ID to recorded lifecycle, observed tmux body,
authoritative task, current gate, and recent pane evidence. Keep these states
separate:

- `recorded`: Playbook's durable lifecycle record;
- `observed`: current tmux body and visible pane evidence;
- `sent`: Playbook submitted text and Enter to the pane;
- `acknowledged`: recipient visibly received or responded to the message;
- `acted on`: subsequent evidence shows behavior or state changed.

A send proves only `sent`. Silence proves neither idleness nor completion.
Task ownership comes from Playbook task authority, not a pane claim or a plan's
informal marker. If evidence disagrees, report the disagreement before steering.

## Observe with restraint

Inspect a session when the user asks, at an agreed cadence, or when another
public fact suggests attention is needed. Look for:

- duplicate task ownership or a worker acting outside its authoritative task;
- a gate/CLI disagreement, repeated failure, approval wait, or idle prompt;
- a cross-session dependency the workers cannot see;
- a project-wide quality or intent issue visible only across tasks.

Healthy work needs no message. Do not interrupt merely to demonstrate activity.
Prefer a compact fleet report: session, task/gate, observed state, evidence,
needed action. Distinguish direct evidence from inference.

## Steer one recipient

Send only to the intended managed session:

```bash
pb-session send NAME "OPERATOR: concise instruction"
pb-session peek NAME 40
```

Serialize messages to one recipient. Text leaving the input box or appearing as
submitted output proves observation beyond the send receipt, not acknowledgment.
Require an explicit recipient response or subsequent recipient action before
calling it acknowledged. If it remains unconsumed, report `sent, not
acknowledged`; do not repeatedly inject duplicates.
Never let workers message or inspect each other unless the user explicitly wants
peer coordination. Central observation avoids hidden cross-lane authority.

For duplicate work, establish the authoritative owner before redirecting anyone.
Do not revert overlapping edits automatically. Ask the non-owner to stop and
surface any already-made changes for the owner or user to reconcile.

## Preserve inspectability

Use `pb-session stop NAME` only with authority. A stopped or unexpectedly exited
body remains inspectable and its native provider session remains resumable.
Do not destroy it for tidiness. After reboot, reconstruct the working set with
`pb-session list --all` and `pb-session status NAME`; recommend exact manual
resumes, never automatic resurrection.

## Finish

Return a compact account of:

- what each session was doing;
- what was directly observed versus inferred;
- messages sent and whether they were acknowledged or acted on;
- collisions, stalls, authority questions, and monitor mistakes;
- the smallest next action for the user or each worker.

Distill a culture candidate only when requested or explicitly authorized. Keep
only a cross-project lesson that would lower future coordination cost; leave
session-specific facts in the task and chat history.
