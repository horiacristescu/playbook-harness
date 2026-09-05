# Sessions and monitoring

A normal agent conversation is enough for short work. A managed session is
useful when the work lasts longer, when another agent must observe it, or when
you want a reliable way back after an interruption.

## What a managed session adds

`pb-session` starts an agent inside a Playbook-owned tmux server.

The session combines three handles, a human name, the provider's native
conversation identity, and the managed tmux body, and Playbook records their
relationship so the human does not have to keep it in memory.

A zero-argument launch uses the project's saved provider and attaches:

```bash
pb-session
```

Direct launches such as `claude`, `codex`, or root-launched `omp` remain
supported. Bootstrap records those conversations as ad-hoc sessions, without a
managed tmux body.

## Find existing sessions

```bash
pb-session list
```

The list separates recorded lifecycle state from what Playbook can currently
observe in tmux.

```bash
pb-session status <name-or-native-id>
```

Status shows one session in more detail, and when the body is gone it prints
the provider-native identity and an exact manual resume route.

A session may be recorded but no longer running, or a tmux body may be alive
while the agent inside it has stopped. Playbook reports what it knows instead
of turning those states into one vague label.

## Observe and steer

`pb-tmux-agent` provides the lower-level transport used by managed work.

```bash
pb-tmux-agent peek reviewer 50
pb-tmux-agent send reviewer "review the failing test"
pb-tmux-agent attach reviewer
pb-tmux-agent tail reviewer 50
pb-tmux-agent wait reviewer --timeout 30
```

`peek` reads recent terminal output, `send` delivers a message, and `attach`
lets a human enter the session directly.

A message being sent does not prove it was read, and a response proves
acknowledgment rather than action. For important steering, check the later
work.

## Recover after interruption

The session record helps you find a conversation again. It does not restart
the process.

After a reboot, `pb-session list` can still show which conversations existed,
and `pb-session status` provides the native route needed to resume one. The
human or agent then decides which sessions are still worth restoring.

Playbook does not keep a second transcript. The provider owns conversation
history; Playbook keeps the small amount of state needed to find it.

## Use a monitor for several agents

A monitor is an ordinary Playbook agent with a normal owned task. Create its
blackboard with `pb-tasks new monitor <name> [intent]`. The task keeps evolving
user intent, incoming requests, watcher recovery, and one section per lane.
Unlike a build plan, lane assignment gates may close in the order results
arrive. They open when work is dispatched and close after collection and
validation with the monitor's report.

Individual agents naturally focus on their current gates, while the monitor
watches across tasks and sessions for collisions, repeated failures,
misalignment with project intent, or evidence that may disappear.

The monitor uses the same `pb-session` interface as the human. It does not get
special authority over task ownership or approvals.

Live coverage also needs a wake mechanism exposed by the current provider.
Verify a real event and record its limits on the board. Quiet is a reason to
inspect, not proof that a lane stalled. Restore worker sessions and the watcher
as separate steps after a restart.

A healthy monitor leaves healthy work alone and steps in for cases like those:
two agents in the same file, one agent rerunning a failing command, or work
drifting from what the task said.

The monitor should distinguish five kinds of evidence:

- recorded state;
- live observation;
- a message that was sent;
- an acknowledgment;
- evidence that the requested action happened.

## Keep the roles separate

Managed tmux provides persistence, observation, and steering. `pb-sandbox`
provides filesystem containment. Tests and reviews provide evidence about the
result. These parts can be used together, but none substitutes for the others.

## Current transport boundary

The managed tmux server is separate from the user's ordinary tmux server.
Project initialization creates no tmux state and does not change the user's
tmux configuration.

Each managed body uses one session, one window, and one pane. Dead panes retain
exit evidence. Cleanup owns the foreground process group. A child that escapes
that group with `setsid()` is outside tmux cleanup and must be contained by a
sandbox when that risk matters.
