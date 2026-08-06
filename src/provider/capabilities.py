"""
ProviderCapabilities — runtime-detected flags per provider instance.
SessionFacts — persisted session state loaded fresh each hook call.

Capability detection runs once at session start in ProviderAdapter.detect_capabilities().
It is never re-run mid-session. Policy functions receive capabilities as read-only input.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ProviderCapabilities:
    """
    Runtime-detected capabilities for one provider instance.

    Flags are detected from the environment (hook stdin schema, CLI version,
    on-disk config files) — never hardcoded per provider name. Two instances
    of the same provider on different machines or versions may differ.

    Detection boundary: capability detection ends here. Policy functions receive
    a ProviderCapabilities instance and never probe the environment themselves.
    """

    provider: str
    """Provider identifier: "claude" | "codex" | "antigravity" | "unknown"."""

    has_user_prompt_hook: bool
    """Provider fires a scriptable hook before each user message is processed.
    Claude: True (UserPromptSubmit). Codex: True when the hooks feature is enabled. Antigravity: unknown."""

    has_pre_tool_hook: bool
    """Provider fires a scriptable hook before each tool call, allowing hard blocks.
    Claude: True (PreToolUse, exit 2 = block). Codex: Bash-only when the hooks feature is enabled. Antigravity: unknown."""

    has_post_tool_hook: bool
    """Provider fires a scriptable hook after each tool call (observe, no block).
    Claude: True (PostToolUse). Codex: Bash-only when the hooks feature is enabled. Antigravity: unknown."""

    has_stop_hook: bool
    """Provider fires a scriptable hook at session end, allowing exit to be blocked.
    Claude: True (Stop). Codex: True when the hooks feature is enabled. Antigravity: AfterAgent (unverified/advisory)."""

    session_id_in_payload: bool
    """Provider injects session_id into hook stdin JSON payload.
    Claude: True. Codex: False (use SQLite + PID-walk). Antigravity: unknown."""

    session_log_format: str
    """Format of on-disk session log: "jsonl" | "json" | "none" | "unknown".
    Claude: "jsonl". Codex: "jsonl". Antigravity: "unknown" (no files found)."""

    session_log_base: Optional[Path]
    """Root directory for session log discovery, or None if unavailable.
    Claude: ~/.claude/projects/<slug>/. Codex: ~/.codex/sessions/. Antigravity: None."""


@dataclass
class SessionFacts:
    """
    Persisted session state — loaded fresh from disk on each hook call.
    No in-memory continuity between calls; this is the entire session state.

    Written by: tasks CLI (active_task_*), chat-log-hook (chat_log_offset),
                hook counter scripts (tool/write counts via separate counter file).
    Read by: all policy functions, all hook scripts.

    Survives: context compaction, provider restart (same session_id),
              concurrent sessions (each session has its own file).
    """

    session_id: str
    """Unique identifier for this provider session.
    Claude: from hook stdin payload. Codex/Antigravity: wrapper UUID or pid-<N> fallback."""

    project_root: Path
    """Absolute path to project root (contains .agent/tasks/).
    Derived from find_project_root() walk, not $PWD directly."""

    active_task_number: Optional[int] = None
    """Task number currently active (tasks work <N>), or None if no task active."""

    active_task_path: Optional[Path] = None
    """Absolute path to active task.md, or None."""

    chat_log_offset: int = 0
    """Byte offset for next incremental read of the provider session log.
    Used by read_new_messages() to avoid re-processing seen messages."""
