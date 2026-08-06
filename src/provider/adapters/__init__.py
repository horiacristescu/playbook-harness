"""Concrete provider adapter implementations."""
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .antigravity import AntigravityAdapter

__all__ = ["ClaudeAdapter", "CodexAdapter", "AntigravityAdapter"]
