---
name: monitor
description: Supervise one or more managed Playbook sessions through pb-session. Use when the user asks to monitor, coordinate, inspect, recover, or steer agent lanes, or adopt an existing session into a managed lane.
---

# Monitor

Be the user's direct report and continuing collaborator. Track what they want, what each lane is doing, and whether the two still agree. Move approved work forward, judge deliveries, coordinate dependencies, and leave healthy work alone.

Use a normal Monitor task as the durable blackboard. Create one with `pb-tasks new monitor NAME` when no related monitor task exists. Its template defines intent, incoming work, watcher state, lane recovery data, assignments, reports, and shutdown. Capture new requests immediately; organize and dispatch them separately.

Read [philosophy.md](philosophy.md) when establishing the arrangement, interpreting the user's intent, or reconsidering whether to intervene, verify, compact, or escalate. It is the canonical statement of Monitor judgment. Routine session operations can use this file directly.

## Establish state and authority

Read the user's request, project guidance, and existing monitor board. Preserve the authorized queue, delegated decisions, choices still belonging to the user, and direct steering they gave a lane. Discussion can be recorded before it is ready to dispatch.

Use public state:

```bash
pb-session list --json
pb-tasks status
pb-session status NAME --json
pb-session peek NAME 40
```

Relate the managed name and native identity to the authoritative task, current work, and recent evidence. Keep these facts separate:

- **Recorded:** durable session lifecycle.
- **Observed:** current process and visible activity.
- **Sent:** text and Enter were submitted.
- **Acknowledged:** the recipient demonstrated receipt.
- **Acted on:** later evidence demonstrated the requested change.

A running process does not prove progress. Silence proves neither idleness nor completion. Task authority outranks a pane claim. Resolve conflicting identity or ownership before redirecting affected work.

Each lane owns its implementation and execution record. Follow the project's task rules yourself. Taking over implementation requires a clear handoff and a decision about who watches the other lanes meanwhile.

## Establish and verify watching

Choose from capabilities actually exposed in the current provider and record the choice on the board. When the user asks for a five-minute check, arm a recurring wakeup now; a promise to remember the cadence is not a watcher.

- When Claude's background `Monitor` tool is available, prefer it for live watching because its events can arrive while the conversation continues. Make watcher failure and lost visibility emit events; do not turn an unchanged pane into a conclusion.
- In Claude, arm a recurring schedule with the scheduling tool it exposes (for example a five-minute cron job), or ask the user to type `/loop 5m` with the prompt; an agent cannot type a slash command itself. Use an explicit prompt such as `Reconcile the monitor board with every assigned lane; inspect changes, blockers, and deliveries, then act or report.` Verify that the schedule exists through the provider's schedule listing. It fires between turns; a long turn can delay a check. Record its handle and re-arm it after expiry or restart.
- In Codex, run `pb-codex loop 5m 'MONITOR: Reconcile the board with every assigned lane; inspect changes, blockers, and deliveries, then act or report.'` from the monitor conversation with escalated permissions. Its background process calls `codex queue`, which the sandbox blocks, so a sandboxed start refuses. It targets the current `CODEX_THREAD_ID` and stops when the Codex window it started from closes, so a closed monitor is never prompted headless. Arming again on the same thread replaces the loop. Run `pb-codex loop list` to record the loop ID, send count, and any error, and `pb-codex loop stop <loop-id>` at shutdown. The first tick arrives after five minutes. Codex delivers queued prompts after the current turn, so long turns can accumulate ticks. After a restart or reboot, arm and verify it again.
- Codex `/goal`, when available, preserves the monitoring objective across turns but does not wake the agent on a timer.
- Other providers may expose different mechanisms. Verify an actual test event instead of inferring coverage from configuration.

Use public `pb-session` observations inside a watcher rather than assuming a managed lane name is a raw tmux target. Record the mechanism, cadence, handle, coverage, and recovery steps. Verify the first received wakeup and resulting lane check, not just the schedule or send receipt. After session exit or reboot, re-arm and test watching; restoring worker conversations does not establish that supervision resumed.

## Observe, decide, act

On a requested check, wake event, useful work boundary, or material new user message, reconcile the board with current evidence. When a lane appears quiet, determine why:

- Resume unnecessarily stopped work within its approved scope.
- Collect a completed delivery and decide whether follow-up is needed.
- Resolve an approval or blocker already within your delegation.
- Preserve a genuine wait for the user or another dependency.

Give attention to decisions about to spread, doubtful claims, repeated failures, shared dependencies, and changes that may undermine the purpose. Matching lane reports can share a mistaken assumption. Inspect representative work as well as outliers. Do not interrupt merely to demonstrate activity.

Before calling a lane free, reconcile its task, delivery, blockers, and owned unfinished changes. A lane's completion claim begins collection; it does not replace validation. Understand what important tests or artifacts actually establish. Record consequential judgments and remaining uncertainty on the board.

Use a fresh judge when an important interpretation or delivery benefits from distance. Let it derive intent from the user's words and decisions before seeing the plan and result. Triage its findings against the sources; the judge advises, and may also misunderstand.

## Dispatch and verify

Write the assignment gate as the work is sent. Keep the outcome and necessary constraints together. Separate unrelated asks when a lane has dropped secondary requests.

```bash
pb-session send NAME 'MONITOR: Continue the approved task at its current gate.'
pb-session peek NAME 40
```

A lane is a shell that outlives its agent. `send` refuses when no agent runs there, because the text would execute as a shell command. `pb-session swap NAME PROVIDER` saves a handoff and starts another provider in the same shell. `pb-session stop NAME` ends the agent and keeps the shell; `pb-session destroy NAME` closes the shell. Use these only with authority.

Preserve arbitrary message text through shell quoting; never interpolate it into executable shell syntax. Serialize messages to one recipient. If input remains unconsumed, inspect before retrying so duplicates do not create duplicate work.

Close the assignment gate only after collection and validation, appending the result, evidence, judgment, and remaining work. A rejected or partial delivery can still be collected; keep the broader user outcome open and create a new correction gate.

For overlap, establish the authoritative owner and coordinate the correction. Do not automatically revert another session's work or sweep shared changes into one commit. Keep coordination visible and report your own mistaken routing, interruption, or inference.

## Compact and recover

Keep related repair and verification in the same episode. At a clean boundary, preserve the task, decisions, evidence, blockers, relevant standard, and next action before asking the provider to compact. Token count or one mistake can prompt assessment; neither proves exhaustion.

For a session started outside managed tmux, obtain its provider-native identity from that session:

```bash
echo $PLAYBOOK_SESSION_ID
pb-session adopt claude:<native-id> --name lane-x
```

Have the user exit the original session before adoption. Adoption is same-project and trusts that exit; it does not prove liveness. Never guess identity from timestamps. Confirm status and visible context before steering.

A reboot destroys the tmux boxes but not the session records, this board, or the providers' saved conversations. The monitor usually runs outside tmux, so it comes back first. The user opens one terminal in the project and runs:

```bash
pb-tasks resume monitor
```

It resumes the conversation that owns the one open monitor task, with a recovery prompt, and refuses when several are open; `pb-tasks resume N` names the task instead. Any task's conversation comes back the same way, and `pb-tasks resume` alone lists open tasks with their sessions.

Then recover the lanes with `pb-session list --all` and `pb-session status NAME`. A reboot closes every lane but keeps its last conversation. `pb-session resume NAME` builds a new lane with the same name and resumes that conversation in it. Compare the native ID that status reports with the one on the board first: a swap moves a lane to a new conversation. `pb-session resume provider:native-id` resumes an exact conversation instead. Resume within the user's existing authorization, preserve stopped conversations for inspection, confirm identity and model, update the recovery gate, then re-arm watching. Inside a managed lane, `$PLAYBOOK_SESSION_NAME` holds its lane name.

## Known limits

- Quiet requires inspection; it cannot classify thinking, waiting, completion, and failure by itself.
- Send receipts do not prove acknowledgment or action.
- Watch availability, timing, and restoration differ by provider and version.
- Context exhaustion has warning signs but no reliable universal threshold.
- Monitoring can lapse while the monitor is absorbed in implementation or a long turn.
- Resume needs the provider's saved conversation. Claude deletes conversations older than `cleanupPeriodDays` (default 30 days).

## Report

Keep the user informed with short, simple, synthetic reports. Lead with what happened and why it matters, then give the evidence or uncertainty needed to judge trust. Keep detail on the board. Do not forward unexamined lane claims, repeat unchanged status, or hide a material limit or monitor mistake for the sake of brevity.
