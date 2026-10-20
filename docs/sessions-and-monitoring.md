# Sessions and monitoring

A normal agent conversation is enough for short work. A managed session is
useful when the work lasts longer, when another agent must observe it, or when
you want a reliable way back after an interruption.

## What a managed session adds

`pb-session` opens a lane: a shell in a Playbook-owned tmux server, with an
agent typed into it. The shell outlives the agent. You can stop the agent and
keep the lane, start a different one in the same shell, or attach and use the
shell yourself.

A lane joins three handles: its human name, the provider's native conversation
identity, and the tmux body. Playbook records how they relate, so you don't
have to remember it.

A zero-argument launch uses the project's saved provider and attaches:

```bash
pb-session
pb-session --name lane-a --provider codex --detach
```

The command returns once the agent's start command is typed. The agent binds
its conversation a moment later. Until then `status` lists the lane with no
session ID, and it names any startup screen that needs a person, such as a
folder-trust question. Playbook never answers those screens itself.

Direct launches such as `claude`, `codex`, or root-launched `omp` remain
supported. Bootstrap records those conversations as ad-hoc sessions, without a
managed tmux body.

## Find existing sessions

```bash
pb-session list
```

Each row shows the conversation's lifecycle. A lane row also shows the lane
state. After a swap, the earlier conversation keeps its own row, marked
stopped, even though the lane's shell is still alive.

```bash
pb-session status <name-or-native-id>
```

Status shows one session in more detail, and when the body is gone it prints
the provider-native identity and an exact manual resume route.

A session may be recorded but no longer running, or a tmux body may be alive
while the agent inside it has stopped. Playbook reports what it knows instead
of turning those states into one vague label.

## Observe and steer

For a lane, use its name:

```bash
pb-session peek lane-a 50
pb-session send lane-a "review the failing test"
pb-session attach lane-a
```

`send` types only into a running agent. With no agent in the foreground the
text would run as a shell command, so it refuses. Attach to work in the shell
yourself.

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

### Switch agents in a lane

```bash
pb-session swap lane-a claude
```

`swap` hands a lane to another provider without closing its shell. It first
saves a handoff file under `.agent/sessions/.handoffs/`. The file holds the
lane's last 500 screen lines and the last 200 lines of `.agent/chat_log.md`.
Then it stops the agent and starts the new one in the same shell.

The new agent's first message names the outgoing session. If that session
owned a task, the message tells the agent to claim it with
`pb-tasks work N --handoff-from <old-key>`. That claim succeeds only if the
task still names the outgoing session as owner. Otherwise the new agent stops
and tells you. Swap refuses when the outgoing conversation looks open in
another terminal, and when it owns more than one task.

### Stop or close a lane

`pb-session stop lane-a` ends the agent; the shell stays open.
`pb-session destroy lane-a` closes the shell and refuses while an agent still
runs. Neither forgets the conversation.

### Remind a regular Codex session

From inside a Codex conversation, start a recurring reminder without a managed
tmux lane:

```bash
pb-codex loop 5m Check the monitor board and assigned lanes
pb-codex loop list
pb-codex loop stop <loop-id>
```

Run the first command with escalated permissions. The loop's background
process calls `codex queue`, and the Codex sandbox blocks that call, so a
sandboxed start refuses and asks for escalation. `list` and `stop` work from
inside the sandbox.

The loop uses the current `CODEX_THREAD_ID`. From another shell, supply
`--thread <codex-thread-uuid>` explicitly. A detached local process calls
`codex queue` at each interval; Codex handles queued input after its current
turn. The first reminder arrives after one interval. `list` shows send counts
and the last error. Long Codex turns can leave several reminders queued.

A loop lives only as long as the Codex window it was started from. Codex keeps
a conversation open in its background service after the window closes, and it
would keep answering queued reminders with nobody watching. So the loop finds
the window at start and stops when that window exits. A window that resumed
the thread by ID is matched exactly. Otherwise, every Codex window working in
the same directory counts, and the loop runs until the last of them closes.
Pass `--owner <pid>` when it cannot find the window. Arming again on the same
thread replaces the earlier loop. A restarted session starts with no loop and
must arm a new one, and so must a session after a reboot.

## Recover after interruption

The session record helps you find a conversation again. It does not restart
the process.

After a reboot, `pb-session list` can still show which conversations existed,
and `pb-session status` provides the native route needed to resume one. The
human or agent then decides which sessions are still worth restoring.

`pb-session resume lane-a` resumes the lane's last conversation. If the lane's
shell is idle, the conversation comes back in that shell. If a reboot ended
the shell, Playbook builds a new lane with the same name. When you don't know
which conversation you were in, `pb-session restore` lists recent ones with
their tasks and your last messages. See the
[command reference](command-reference.md).

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
