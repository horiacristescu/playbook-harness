"""
Pure policy functions — map (capabilities, facts, event) -> Decision.

These functions have no side effects. They do not write files, call hooks,
or probe the environment. All inputs are loaded by the adapter before calling.

Integration: spec-only in T111. These are not called by any hook today.
T112 will wire hook scripts to call these via a thin Python entry point.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Literal, Optional

from .capabilities import ProviderCapabilities, SessionFacts
from .events import MessageEvent, ToolEvent, StopEvent


@dataclass(frozen=True)
class Decision:
    """
    Output of a policy evaluation — what should happen next.

    "allow"  — proceed normally, no intervention.
    "warn"   — proceed but surface a message to the agent (stdout).
    "block"  — prevent the action; hook exits with code 2 (Claude PreToolUse).
               For providers without hard-block capability, block degrades to warn.
    "skip"   — capability absent for this provider; do nothing silently.
               Distinct from allow: skip means "this hook point doesn't exist here",
               not "this action is approved".
    """
    action: Literal["allow", "warn", "block", "skip"]
    message: Optional[str] = None

    @classmethod
    def allow(cls) -> "Decision":
        return cls(action="allow")

    @classmethod
    def warn(cls, message: str) -> "Decision":
        return cls(action="warn", message=message)

    @classmethod
    def block(cls, message: str) -> "Decision":
        return cls(action="block", message=message)

    @classmethod
    def skip(cls) -> "Decision":
        """Capability absent — do nothing. Not an approval, just a no-op."""
        return cls(action="skip")


def evaluate_message(
    caps: ProviderCapabilities,
    facts: SessionFacts,
    event: MessageEvent,
) -> Decision:
    """
    Evaluate a user message before or as it is processed.

    Called from: UserPromptSubmit hook (Claude), or after read_new_messages()
    delivers a message (Codex/agy file-based path).

    Side effects: none. The adapter is responsible for writing to chat_log.md.

    Fallback when caps.has_user_prompt_hook is False:
        Return Decision.skip() — message capture happens via the file-based
        read_new_messages() path instead. No enforcement at this point.

    Current policy: always allow (message capture is not a gate point).
    Future: could warn if message references forbidden patterns, or if no
    active task exists and the message looks like a code change request.
    """
    if not caps.has_user_prompt_hook:
        return Decision.skip()

    # Message capture is a recording concern, not a gate. Always allow.
    return Decision.allow()


def evaluate_tool_call(
    caps: ProviderCapabilities,
    facts: SessionFacts,
    event: ToolEvent,
) -> Decision:
    """
    Evaluate a tool call before it executes.

    Called from: PreToolUse hook (Claude only today).

    NOT called for Codex (no scriptable pre-tool hook — prefix_rule approval
    only) or agy (hook model unverified). If called for a provider where
    caps.has_pre_tool_hook is False, return Decision.skip().

    Current policy stub — full logic lives in task-gate-hook bash until T112:
        - If no active task and tool touches a code file: block.
        - Otherwise: allow.

    The bash hook (task-gate-hook) is the authoritative implementation today.
    This stub defines the intended Python interface for T112 wiring.
    """
    if not caps.has_pre_tool_hook:
        return Decision.skip()

    code_tools = {"Edit", "Write", "MultiEdit"}
    if event.tool_name in code_tools and facts.active_task_number is None:
        # Allow edits to task-management directories without an active task
        if _is_management_path(event.file_path):
            return Decision.allow()
        if _is_code_file_path(event.file_path):
            return Decision.block(
                "No active task. Run `pb-tasks work <N>` before editing code."
            )

    return Decision.allow()


def _is_management_path(file_path: str) -> bool:
    """Return True if path is under .agent/ or .claude/ (always allowed without task)."""
    import os
    norm = file_path.replace("\\", "/")
    parts = norm.split("/")
    return ".agent" in parts or ".claude" in parts


def _is_code_file_path(file_path: str) -> bool:
    """Return True if path looks like a code file (should require active task).

    Mirrors task-gate-hook is_code_file_path: extensions, scripts/bin/src/hooks
    directories, and shebang detection (shebang not checked here — hooks only).
    """
    import os
    _CODE_EXTENSIONS = {
        ".py", ".ts", ".js", ".tsx", ".jsx", ".sh", ".bash",
        ".go", ".rs", ".rb", ".java", ".c", ".cpp", ".h",
        ".css", ".html", ".sql", ".yaml", ".yml", ".toml",
    }
    _CODE_DIRS = {"scripts", "bin", "src", "hooks", "lib", "cmd"}

    if not file_path:
        return False
    norm = file_path.replace("\\", "/")
    _, ext = os.path.splitext(norm)
    if ext.lower() in _CODE_EXTENSIONS:
        return True
    parts = set(norm.split("/"))
    return bool(parts & _CODE_DIRS)


def evaluate_stop(
    caps: ProviderCapabilities,
    facts: SessionFacts,
    event: StopEvent,
) -> Decision:
    """
    Evaluate session end — the universal enforcement point.

    Called from: Stop hook (Claude), AfterAgent (agy, unverified),
    or session-end equivalent for other providers.

    This is the ONLY enforcement point available across all providers that
    have any hook support. If a provider lacks this, Playbook is advisory only.

    Fallback when caps.has_stop_hook is False:
        Return Decision.skip() and log a warning — enforcement was not possible.
        Do not silently omit: the user should know enforcement was bypassed.

    Current policy stub — checks for open gates in active task:
        - If active task has unchecked gates: warn (or block if hard enforcement).
        - If no active task: allow (session may have been purely exploratory).
    """
    if not caps.has_stop_hook:
        return Decision.skip()

    if facts.active_task_number is None:
        return Decision.allow()

    # Full gate-checking logic will be wired in T112.
    # Stub: always allow at stop to avoid disrupting existing Claude behavior.
    # The bash stop-hook is authoritative until T112.
    return Decision.allow()
