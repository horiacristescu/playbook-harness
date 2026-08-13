#!/usr/bin/env python3
"""Locked writer used by the Bash UserPromptSubmit bridge."""

from __future__ import annotations

from pathlib import Path
import sys


RUNTIME_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RUNTIME_ROOT / "src"))

from tasks.chat_state import (  # noqa: E402
    append_chat_event,
    append_chat_message,
    derive_event_key,
)


def main() -> int:
    if len(sys.argv) == 7 and sys.argv[1] == "event":
        agent_dir, provider, session_id, timestamp, marker = sys.argv[2:]
        message = sys.stdin.read()
        append_chat_event(
            Path(agent_dir),
            marker,
            provider,
            session_id,
            timestamp,
            message,
            event_key=derive_event_key(marker, provider, session_id, message),
        )
        return 0
    if len(sys.argv) != 5:
        print(
            "usage: chat-log-write.py AGENT_DIR PROVIDER SESSION_ID TIMESTAMP\n"
            "   or: chat-log-write.py event AGENT_DIR PROVIDER SESSION_ID TIMESTAMP MARKER",
            file=sys.stderr,
        )
        return 2
    message = sys.stdin.read()
    append_chat_message(Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
