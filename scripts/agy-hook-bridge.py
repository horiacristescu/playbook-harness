#!/usr/bin/env python3
"""Translate Antigravity's hook protocol to Playbook's Bash hook protocol."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


RUNTIME_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RUNTIME_ROOT / "src"))

from provider.session_identity import (  # noqa: E402
    NativeSessionIdentityError,
    hook_session_id,
)
from provider.session_state import ensure_session_record  # noqa: E402
from tasks.core import resolve_agent_dir  # noqa: E402


def _project_root(payload: dict[str, Any]) -> Path | None:
    for raw in payload.get("workspacePaths", []):
        if not isinstance(raw, str):
            continue
        candidate = Path(raw)
        if (candidate / ".agent" / "tasks").is_dir():
            return candidate
        agent_dir = candidate / ".agent"
        if agent_dir.is_dir() and any(
            (child / "tasks").is_dir() for child in agent_dir.iterdir()
        ):
            return candidate
    return None


def _tool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        tool_call = {}
    name = tool_call.get("name", "")
    args = tool_call.get("args")
    if not isinstance(args, dict):
        args = {}

    aliases = {
        "run_command": "Bash",
        "write_to_file": "Write",
        "replace_file_content": "Edit",
        "multi_replace_file_content": "MultiEdit",
        "view_file": "Read",
        "grep_search": "Grep",
        "find_by_name": "Glob",
        "search_web": "WebSearch",
        "read_url_content": "WebFetch",
    }
    tool_input = dict(args)
    if name == "run_command":
        tool_input["command"] = args.get("CommandLine", "")
    elif name == "write_to_file":
        tool_input["file_path"] = args.get("TargetFile", "")
        tool_input["content"] = args.get("CodeContent", "")
    elif name == "replace_file_content":
        tool_input["file_path"] = args.get("TargetFile", "")
        tool_input["old_string"] = args.get("TargetContent", "")
        tool_input["new_string"] = args.get("ReplacementContent", "")
    elif name == "multi_replace_file_content":
        target = args.get("TargetFile")
        chunks = args.get("ReplacementChunks")
        if not isinstance(target, str) or not target or "\x00" in target or "\n" in target:
            raise ValueError("Agy multi_replace_file_content requires one valid TargetFile")
        if not isinstance(chunks, list) or not chunks:
            raise ValueError("Agy multi_replace_file_content requires ReplacementChunks")
        tool_input["file_path"] = target
        tool_input["edits"] = chunks

    result = dict(payload)
    result["tool_name"] = aliases.get(str(name), str(name))
    result["tool_input"] = tool_input
    return result


def _missing_identity_response(event: str, payload: dict[str, Any], exc: Exception) -> dict[str, Any]:
    """Fail safely when Agy omits a native ID (notably in some --print hooks).

    Missing identity must never authorize a mutation or shell command. Read-only
    provider tools may continue under Agy's own permission policy, and Stop must
    release rather than trapping the model in an unfinishable retry loop.
    """
    reason = f"Playbook identity unavailable: {exc}"
    if event == "pre-tool":
        tool_call = payload.get("toolCall")
        name = tool_call.get("name", "") if isinstance(tool_call, dict) else ""
        read_only = {"view_file", "grep_search", "find_by_name", "search_web", "read_url_content"}
        if name in read_only:
            return {"decision": "deny_unless_prior_grant", "reason": reason}
        return {"decision": "deny", "reason": reason}
    if event == "stop":
        return {"decision": "stop", "reason": reason}
    if event == "pre-invocation":
        return {"injectSteps": []}
    return {}


def _run_common(
    script: str,
    payload: dict[str, Any],
    project: Path,
    native_id: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # Agy may be launched from inside another provider's agent process. Do not
    # let that parent's native identity override the bridge's validated Agy
    # identity when the shared shell hooks select their provider dialect.
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop("CODEX_THREAD_ID", None)
    env.pop("PLAYBOOK_SESSION_ID", None)
    env["ANTIGRAVITY_CONVERSATION_ID"] = native_id
    env["PLAYBOOK_BRIDGE_PROVIDER"] = "antigravity"
    env["PLAYBOOK_PROVIDER"] = "antigravity"
    env["PLAYBOOK_PROJECT_ROOT"] = str(project)
    return subprocess.run(
        [str(RUNTIME_ROOT / "scripts" / script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=project,
        env=env,
        check=False,
    )


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    payload: dict[str, Any] = {}
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        native_id = hook_session_id("antigravity", payload, os.environ)
    except (ValueError, NativeSessionIdentityError, json.JSONDecodeError) as exc:
        print(json.dumps(_missing_identity_response(event, payload, exc)))
        return 0

    project = _project_root(payload)
    if project is None:
        if event == "pre-tool":
            print(json.dumps({"decision": "deny_unless_prior_grant"}))
        elif event == "pre-invocation":
            print(json.dumps({"injectSteps": []}))
        elif event == "stop":
            print(json.dumps({"decision": "stop"}))
        else:
            print("{}")
        return 0

    # Pre-invocation is Agy's earliest project-aware identity event. Publish
    # the same minimal record used by all other providers before any branch.
    ensure_session_record(resolve_agent_dir(project), "antigravity", native_id)

    if event == "pre-invocation":
        # Identity is deliberately validated at the earliest available Agy
        # lifecycle event. Agy has no UserPromptSubmit-equivalent payload.
        print(json.dumps({"injectSteps": []}))
        return 0

    if event == "pre-tool":
        try:
            translated = _tool_payload(payload)
        except ValueError as exc:
            print(json.dumps({"decision": "deny", "reason": f"Playbook tool payload invalid: {exc}"}))
            return 0
        result = _run_common("task-gate-hook", translated, project, native_id)
        if result.returncode == 2:
            reason = result.stderr.strip() or "Blocked by Playbook task policy"
            print(json.dumps({"decision": "deny", "reason": reason}))
        else:
            # Preserve Agy's own permission policy instead of auto-approving.
            print(json.dumps({"decision": "deny_unless_prior_grant"}))
        return 0

    if event == "post-tool":
        try:
            translated = _tool_payload(payload)
        except ValueError:
            print("{}")
            return 0
        _run_common("state-echo-hook", translated, project, native_id)
        print("{}")
        return 0

    if event == "stop":
        result = _run_common("stop-hook", payload, project, native_id)
        if result.returncode == 2:
            print(json.dumps({
                "decision": "continue",
                "reason": result.stderr.strip() or "Playbook task remains open",
            }))
        else:
            print(json.dumps({"decision": "stop"}))
        return 0


    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
