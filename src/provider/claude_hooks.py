"""Render durable project-local Claude hooks for a standalone runtime.

Commands point at the resolved central runtime, so bare CLI, IDE, and headless
invocations share one integration. Project reconciliation is implemented in a
later task.
"""

from __future__ import annotations

import shlex
import json
from pathlib import Path


HOOK_TIMEOUT_MS = 5_000


def _entry(
    scripts_dir: Path,
    script_name: str,
    *,
    matcher: str | None = None,
    status_message: str | None = None,
) -> dict:
    command = shlex.join([str(scripts_dir / script_name)])
    hook: dict = {
        "type": "command",
        "command": command,
        "timeout": HOOK_TIMEOUT_MS,
    }
    if status_message is not None:
        hook["statusMessage"] = status_message
    entry: dict = {"hooks": [hook]}
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def render_playbook_hooks(runtime_root: Path) -> dict:
    """Return the Playbook-owned Claude settings hook fragment.

    ``runtime_root`` is resolved before commands are persisted so bare Claude,
    IDE integrations, and headless invocations do not need a wrapper or plugin
    loader to inject environment variables.
    """
    root = runtime_root.expanduser().resolve()
    scripts = root / "scripts"
    spec_path = root / "hooks" / "claude-standalone.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    hooks: dict[str, list[dict]] = {}
    for event, entries in spec["events"].items():
        hooks[event] = [
            _entry(
                scripts,
                item["script"],
                matcher=item.get("matcher"),
                status_message=item.get("statusMessage"),
            )
            for item in entries
        ]
    return {"hooks": hooks}


def render_dispatcher_hooks(dispatcher: str = "pb-tasks") -> dict:
    """Render hooks through the stable launcher installed on the user's PATH."""
    spec_path = Path(__file__).resolve().parents[2] / "hooks" / "claude-standalone.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    hooks: dict[str, list[dict]] = {}
    for event, entries in spec["events"].items():
        rendered = []
        for item in entries:
            hook: dict = {
                "type": "command",
                "command": shlex.join([dispatcher, "hook", item["script"]]),
                "timeout": HOOK_TIMEOUT_MS,
            }
            if item.get("statusMessage") is not None:
                hook["statusMessage"] = item["statusMessage"]
            entry: dict = {"hooks": [hook]}
            if item.get("matcher") is not None:
                entry["matcher"] = item["matcher"]
            rendered.append(entry)
        hooks[event] = rendered
    return {"hooks": hooks}
