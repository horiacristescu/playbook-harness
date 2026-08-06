"""
Provider harness architecture — cross-provider enforcement model.

This package defines the shared data model and policy interface for Claude,
Codex, Antigravity (agy), and pi (local Qwen via oMLX). All concrete provider
logic lives in adapter subclasses; the policy functions here are pure over
loaded facts.

Integration status: spec-only (T111). Bash hooks are unchanged.
Wiring (hooks calling these functions) is T112.

Layout:
    capabilities.py  — ProviderCapabilities, SessionFacts
    events.py        — Event types (MessageEvent, ToolEvent, StopEvent)
    policy.py        — Decision, evaluate_message, evaluate_tool_call, evaluate_stop
    adapter.py       — ProviderAdapter ABC
"""

from .capabilities import ProviderCapabilities, SessionFacts
from .events import MessageEvent, ToolEvent, StopEvent, Event
from .policy import Decision, evaluate_message, evaluate_tool_call, evaluate_stop
from .adapter import ProviderAdapter

__all__ = [
    "ProviderCapabilities",
    "SessionFacts",
    "MessageEvent",
    "ToolEvent",
    "StopEvent",
    "Event",
    "Decision",
    "evaluate_message",
    "evaluate_tool_call",
    "evaluate_stop",
    "ProviderAdapter",
]
