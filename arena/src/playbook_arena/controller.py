"""Deterministic literal-script controller and replayable state machine."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .case import ArenaCaseError
from .schema import ScriptDefinition, ScriptEvent
from .store import RunStore, VerifiedEvent


TERMINAL_WORKER_STATES = frozenset({"completed", "failed", "stopped", "lost"})


@dataclass(frozen=True)
class ControllerState:
    next_index: int
    pending_event_id: str | None
    delivery_requested: bool
    delivery_sent: bool
    output_offset: int
    terminal: str | None


@dataclass(frozen=True)
class ControllerDecision:
    action: str
    event_id: str | None
    reason: str


def replay_controller(script: ScriptDefinition, events: Iterable[VerifiedEvent]) -> ControllerState:
    index_by_id = {event.id: index for index, event in enumerate(script.events)}
    next_index = 0
    pending: str | None = None
    requested = False
    sent = False
    output_offset = 0
    terminal: str | None = None
    for record in events:
        event_id = record.payload.get("event_id")
        if record.type in {"delivery_requested", "delivery_sent", "delivery_acknowledged", "script_event_skipped", "controller_stopped", "controller_failed"}:
            if not isinstance(event_id, str) or event_id not in index_by_id:
                raise ArenaCaseError(f"controller event {record.type} has invalid script event id")
        if record.type == "delivery_requested":
            if terminal or pending is not None or index_by_id[event_id] != next_index:
                raise ArenaCaseError("controller replay found out-of-order delivery request")
            pending, requested, sent = event_id, True, False
        elif record.type == "delivery_sent":
            if pending != event_id or not requested or sent:
                raise ArenaCaseError("controller replay found duplicate/out-of-order send")
            sent = True
            output_offset = record.payload.get("output_offset")
            if type(output_offset) is not int or output_offset < 0:
                raise ArenaCaseError("controller replay found invalid output offset")
        elif record.type == "delivery_acknowledged":
            if pending != event_id or not sent:
                raise ArenaCaseError("controller replay found acknowledgment without send")
            next_index += 1
            pending, requested, sent, output_offset = None, False, False, 0
        elif record.type == "script_event_skipped":
            if terminal or pending not in {None, event_id} or index_by_id[event_id] != next_index:
                raise ArenaCaseError("controller replay found out-of-order skip")
            next_index += 1
            pending, requested, sent, output_offset = None, False, False, 0
        elif record.type == "controller_stopped":
            terminal = "stopped"
        elif record.type == "controller_failed":
            terminal = "failed"
    if next_index > len(script.events):
        raise ArenaCaseError("controller replay advanced beyond script")
    return ControllerState(next_index, pending, requested, sent, output_offset, terminal)


def decide(
    script: ScriptDefinition,
    state: ControllerState,
    *,
    worker_state: str,
    new_output: str = "",
    timed_out: bool = False,
) -> ControllerDecision:
    if state.terminal:
        return ControllerDecision("complete", None, f"controller is {state.terminal}")
    if state.next_index >= len(script.events):
        return ControllerDecision("complete", None, "all script events acknowledged or skipped")
    event = script.events[state.next_index]
    if state.pending_event_id not in {None, event.id}:
        raise ArenaCaseError("controller state pending event does not match script position")
    if state.delivery_sent:
        if re.search(event.acknowledge, new_output, flags=re.MULTILINE):
            return ControllerDecision("acknowledge", event.id, "declared regex matched post-send output")
        if timed_out:
            return ControllerDecision(event.on_timeout, event.id, "declared acknowledgment timeout elapsed")
        if worker_state in TERMINAL_WORKER_STATES:
            return ControllerDecision(event.on_timeout, event.id, f"worker became {worker_state} before acknowledgment")
        return ControllerDecision("wait", event.id, "acknowledgment barrier remains unsatisfied")
    if state.delivery_requested:
        return ControllerDecision("fail", event.id, "delivery was requested but transport send outcome is unknowable after recovery")
    if event.deliver_when == "after_worker_exit" and worker_state not in TERMINAL_WORKER_STATES:
        return ControllerDecision("wait", event.id, "worker has not reached terminal transport state")
    if event.deliver_when == "after_ack" and state.next_index == 0:
        raise ArenaCaseError("after_ack trigger has no predecessor")
    return ControllerDecision("deliver", event.id, "structural script trigger is satisfied")


def _event(script: ScriptDefinition, event_id: str | None) -> ScriptEvent:
    for event in script.events:
        if event.id == event_id:
            return event
    raise ArenaCaseError(f"unknown script event: {event_id}")


def record_decision(store: RunStore, script: ScriptDefinition, decision: ControllerDecision) -> None:
    if decision.action == "wait" or decision.action == "complete":
        return
    event = _event(script, decision.event_id)
    common = {"event_id": event.id, "reason": decision.reason}
    if decision.action == "deliver":
        store.append(
            "delivery_requested",
            {**common, "message_sha256": hashlib.sha256(event.message.encode("utf-8")).hexdigest()},
            state="delivering",
        )
    elif decision.action == "acknowledge":
        store.append("delivery_acknowledged", common, state="controlling")
    elif decision.action == "skip":
        store.append("script_event_skipped", common, state="controlling")
    elif decision.action == "stop":
        store.append("controller_stopped", common, state="stopping")
    elif decision.action == "fail":
        store.append("controller_failed", common, state="failed")
    else:
        raise ArenaCaseError(f"unsupported controller decision: {decision.action}")


def record_sent(store: RunStore, script: ScriptDefinition, event_id: str, *, output_offset: int = 0) -> None:
    event = _event(script, event_id)
    store.append(
        "delivery_sent",
        {"event_id": event.id, "message_sha256": hashlib.sha256(event.message.encode("utf-8")).hexdigest(), "output_offset": output_offset},
        state="awaiting_ack",
    )


def record_send_failure(store: RunStore, script: ScriptDefinition, event_id: str, error: str) -> None:
    _event(script, event_id)
    store.append("controller_failed", {"event_id": event_id, "reason": f"transport send failed: {error}"}, state="failed")
