"""Validate ordinary structured edits to the active task instruction pointer.

Gate checkboxes are Markdown, so agents use their provider-native file editor.
This module keeps workflow invariants behind hooks instead of adding another
agent-facing mutation command.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .task_document import TaskDocument, TaskDocumentError, TaskGateClosure


class GateEditError(ValueError):
    """A proposed structured task edit violates the continuation chain."""


def candidate_from_tool_input(
    original: str, tool_name: str, tool_input: dict[str, Any]
) -> str:
    """Return the complete proposed document for common structured editors."""
    if tool_name == "Write":
        content = tool_input.get("content")
        if not isinstance(content, str):
            raise GateEditError("Write payload has no string content")
        return content

    if tool_name == "Edit":
        old = tool_input.get("old_string")
        new = tool_input.get("new_string")
        if not isinstance(old, str) or not old:
            raise GateEditError("Edit payload has no nonempty old_string")
        if not isinstance(new, str):
            raise GateEditError("Edit payload has no string new_string")
        occurrences = original.count(old)
        if occurrences == 0:
            raise GateEditError("Edit old_string is not present in task.md")
        replace_all = bool(tool_input.get("replace_all", False))
        if occurrences > 1 and not replace_all:
            raise GateEditError(
                f"Edit old_string is ambiguous ({occurrences} occurrences)"
            )
        return original.replace(old, new, -1 if replace_all else 1)

    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list) or not edits:
            raise GateEditError("MultiEdit payload has no edits")
        candidate = original
        for edit in edits:
            if not isinstance(edit, dict):
                raise GateEditError("MultiEdit contains a malformed edit")
            candidate = candidate_from_tool_input(candidate, "Edit", edit)
        return candidate

    raise GateEditError(f"unsupported structured task editor: {tool_name or 'unknown'}")


def _replace_once(text: str, old_lines: list[str], new_lines: list[str]) -> str:
    old = "\n".join(old_lines)
    new = "\n".join(new_lines)
    if old not in text:
        # Patch chunks commonly omit the terminal newline while the file has it.
        old_nl = old + "\n"
        if old_nl not in text:
            raise GateEditError("apply_patch task hunk does not match task.md")
        return text.replace(old_nl, new + "\n", 1)
    if text.count(old) > 1:
        raise GateEditError("apply_patch task hunk is ambiguous")
    return text.replace(old, new, 1)


def candidate_from_apply_patch(original: str, patch: str, target_path: str) -> str:
    """Apply the target's Update File hunks without executing patch code."""
    normalized_target = target_path.replace("\\", "/").lstrip("/")
    lines = patch.splitlines()
    candidate = original
    found = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("*** Update File: "):
            index += 1
            continue
        raw_path = line[len("*** Update File: ") :].strip()
        normalized_path = raw_path.replace("\\", "/").lstrip("/")
        index += 1
        section: list[str] = []
        while index < len(lines) and not lines[index].startswith("*** "):
            section.append(lines[index])
            index += 1
        if not (
            normalized_path == normalized_target
            or normalized_target.endswith("/" + normalized_path)
            or normalized_path.endswith("/" + normalized_target)
        ):
            continue
        found = True
        chunks: list[list[str]] = []
        current: list[str] = []
        for item in section:
            if item.startswith("@@"):
                if current:
                    chunks.append(current)
                    current = []
                continue
            current.append(item)
        if current:
            chunks.append(current)
        for chunk in chunks:
            old_lines: list[str] = []
            new_lines: list[str] = []
            for item in chunk:
                if item.startswith("-"):
                    old_lines.append(item[1:])
                elif item.startswith("+"):
                    new_lines.append(item[1:])
                elif item.startswith(" "):
                    old_lines.append(item[1:])
                    new_lines.append(item[1:])
                else:
                    old_lines.append(item)
                    new_lines.append(item)
            if not old_lines:
                raise GateEditError("apply_patch task hunk has no validation context")
            candidate = _replace_once(candidate, old_lines, new_lines)
    if not found:
        raise GateEditError("apply_patch contains no update for the active task.md")
    return candidate


def validate_task_candidate(original: str, candidate: str) -> None:
    """Enforce authority fields and exactly-first-gate advancement."""
    try:
        before = TaskDocument.parse(original)
        after = TaskDocument.parse(candidate)
    except TaskDocumentError as exc:
        raise GateEditError(str(exc)) from exc

    if after.status != before.status or after.sessions != before.sessions:
        raise GateEditError(
            "ordinary task edits cannot change ## Status or ## Sessions; "
            "use the lifecycle command named by the hook"
        )

    if after.is_monitor_board != before.is_monitor_board:
        raise GateEditError(
            "ordinary task edits cannot add or remove the monitor-board mode marker"
        )

    # Monitor assignments complete according to external events, not document
    # order. The task remains an ordinary owned task, but its body is a
    # blackboard: any lane gate may be updated when that result actually arrives.
    if before.is_monitor_board:
        return

    checked_delta = after.progress[0] - before.progress[0]
    if checked_delta > 1:
        raise GateEditError(
            f"one file edit cannot close multiple gates ({checked_delta} proposed)"
        )

    first_index = next(
        (index for index, gate in enumerate(before.gates) if not gate.checked), None
    )
    if checked_delta == 1:
        if first_index is None:
            raise GateEditError("task has no open gate to close")
        if first_index >= len(after.gates) or not after.gates[first_index].checked:
            raise GateEditError(
                "cannot skip the first open gate; edit the exact gate supplied by the hook"
            )
    elif first_index is not None:
        # Rewording/replanning the current gate is allowed, but deleting it to
        # expose a later gate is an unrecorded skip.
        if len(after.gates) < len(before.gates) and (
            first_index >= len(after.gates)
            or after.gates[first_index].text != before.gates[first_index].text
        ):
            raise GateEditError(
                "cannot remove the first open gate to advance; close it with an outcome"
            )


def gate_closure_from_documents(
    original: str, candidate: str
) -> TaskGateClosure | None:
    """Return the single first-gate transition after validation, if present."""
    validate_task_candidate(original, candidate)
    before = TaskDocument.parse(original)
    after = TaskDocument.parse(candidate)
    if before.is_monitor_board:
        if len(before.gates) != len(after.gates):
            return None
        closures = [
            TaskGateClosure(current.line, prior.text, current.text)
            for prior, current in zip(before.gates, after.gates)
            if not prior.checked and current.checked
        ]
        return closures[0] if len(closures) == 1 else None
    if after.progress[0] - before.progress[0] != 1:
        return None
    first_index = next(
        index for index, gate in enumerate(before.gates) if not gate.checked
    )
    current = after.gates[first_index]
    return TaskGateClosure(current.line, before.gates[first_index].text, current.text)


def validate_structured_task_edit(
    task_file: Path, tool_name: str, tool_input: dict[str, Any]
) -> None:
    original = task_file.read_text(encoding="utf-8")
    candidate = candidate_from_tool_input(original, tool_name, tool_input)
    validate_task_candidate(original, candidate)
