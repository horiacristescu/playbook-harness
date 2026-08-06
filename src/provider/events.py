"""
Normalized event types — provider-specific payloads mapped to a common shape.

Hook scripts parse provider stdin and construct one of these. Policy functions
receive events and never parse raw stdin themselves.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Union


@dataclass(frozen=True)
class MessageEvent:
    """A user message received by the agent.

    Source: UserPromptSubmit hook stdin (Claude), or read_new_messages() file
    read (Codex/agy). Text is the cleaned message content — noise filtered
    (isMeta, slash commands, task-notifications, system-reminders stripped).
    """
    text: str
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass(frozen=True)
class ToolEvent:
    """A tool call the agent is about to make (PreToolUse) or just made (PostToolUse).

    Source: PreToolUse or PostToolUse hook stdin. For providers without pre-tool
    hooks (Codex), ToolEvents are never constructed — evaluate_tool_call() is
    never called.
    """
    tool_name: str
    """Tool name as reported by the provider: "Edit", "Write", "Bash", etc."""

    tool_input: dict[str, Any]
    """Raw tool input dict from hook stdin."""

    file_path: str = ""
    """File path being modified, if applicable (from tool_input). Empty for non-file tools."""

    is_pre: bool = True
    """True if fired before tool execution (can block), False if after (observe only)."""


@dataclass(frozen=True)
class StopEvent:
    """The agent session is ending.

    Source: Stop hook (Claude), AfterAgent (agy), or equivalent session-end
    callback. This is the universal enforcement point available across all providers
    that have any hook support.
    """
    stop_reason: str = ""
    """Provider-specific stop reason string, if available."""


# Union type for all events — use isinstance() checks in policy functions.
Event = Union[MessageEvent, ToolEvent, StopEvent]
