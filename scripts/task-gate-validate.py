#!/usr/bin/env python3
"""PreToolUse validator for ordinary provider-native task.md edits."""

from __future__ import annotations

import json
import sys
from pathlib import Path


RUNTIME_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RUNTIME_ROOT / "src"))

from tasks.gate_edit import GateEditError, validate_structured_task_edit  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: task-gate-validate.py ACTIVE_TASK", file=sys.stderr)
        return 2
    try:
        payload = json.load(sys.stdin)
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            raise GateEditError("structured editor payload has no tool_input")
        validate_structured_task_edit(
            Path(sys.argv[1]), str(payload.get("tool_name", "")), tool_input
        )
    except (OSError, ValueError, GateEditError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
