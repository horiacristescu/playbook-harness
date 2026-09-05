"""
Helpers for Codex lifecycle hook installation and runtime decisions.

Codex hook execution currently lives outside the provider policy stubs: Codex
invokes commands declared in hooks.json directly. This module keeps the logic
pure/testable while the small scripts in scripts/ act as thin entrypoints.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from .policy import _is_code_file_path, _is_management_path
from .session_state import ensure_session_record
from tasks.core import resolve_agent_dir, resolve_session_id as _resolve_shared_session_id

HOOK_TIMEOUT_MS = 5000


class ParseResult:
    """Result of parsing an apply_patch tool_input.command body.

    Two-state output for finding-4 silent-bypass detection:
      had_headers=False: no apply_patch grammar markers seen — not an edit.
      had_headers=True, paths=[]: looked like apply_patch but no paths extracted —
        treat as deny case (refuse without active task) rather than allow,
        otherwise a malformed/new-shape patch silently bypasses the gate.
    """

    __slots__ = ("paths", "had_headers")

    def __init__(self, paths: list[str], had_headers: bool):
        self.paths = paths
        self.had_headers = had_headers

    def __repr__(self) -> str:
        return f"ParseResult(paths={self.paths!r}, had_headers={self.had_headers!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ParseResult):
            return NotImplemented
        return self.paths == other.paths and self.had_headers == other.had_headers


# Tolerate leading whitespace on patch markers — round-tripping through JSON
# pretty-printers or wrappers can indent the body (panel impl-review #E).
_PATCH_MARKER_RE = re.compile(r"^\s*\*\*\* ")
_FILE_HEADER_RE = re.compile(
    r"^\s*\*\*\* (?:Add|Update|Delete) File:\s*(.+?)\s*$"
)
# Codex's rename directive: *** Update File: <old> followed by a *** Move to:
# (or *** Rename to:) directive on a subsequent line. Capture the destination
# so a no-task rename non-code → code path is caught (panel impl-review #B).
_MOVE_TO_RE = re.compile(
    r"^\s*\*\*\* (?:Move|Rename) to:\s*(.+?)\s*$"
)


def parse_patch_paths(command: str) -> ParseResult:
    """Extract file paths from an apply_patch command body.

    Recognizes Codex's canonical apply_patch grammar:
      *** Begin Patch
      *** Add File: <path>
      *** Update File: <path>     (may be followed by *** Move to: <new> for renames)
      *** Delete File: <path>
      *** End Patch

    Returns ParseResult. Never raises.
    """
    if not command:
        return ParseResult(paths=[], had_headers=False)

    paths: list[str] = []
    had_headers = False

    for raw_line in command.splitlines():
        if not _PATCH_MARKER_RE.match(raw_line):
            continue
        # Any "*** " line (with optional leading whitespace) signals apply_patch grammar.
        had_headers = True

        m = _FILE_HEADER_RE.match(raw_line)
        if m:
            path = m.group(1).strip()
            if path:
                paths.append(path)
            continue

        m = _MOVE_TO_RE.match(raw_line)
        if m:
            dst = m.group(1).strip()
            if dst:
                paths.append(dst)
            continue

        # *** Begin Patch / *** End Patch / unrecognized *** directive:
        # keeps had_headers=True; contributes no path. If no per-file
        # headers ever match, paths stays empty → caller treats as deny.

    return ParseResult(paths=paths, had_headers=had_headers)


def resolve_session_id() -> str:
    """Use the same session resolver as pb-tasks."""
    return _resolve_shared_session_id()


def find_project_root(start: Path, declared_root: Path | None = None) -> Path:
    """Find Playbook state without trusting an unrelated inherited root.

    Managed launches declare ``PLAYBOOK_PROJECT_ROOT``, but a provider started
    from another agent can inherit that agent's environment.  The declaration
    is authoritative only while the hook cwd remains inside it; otherwise the
    native child session discovers its own project from cwd.
    """
    resolved_start = start.resolve()
    if declared_root is not None:
        resolved_declared = declared_root.resolve()
        if resolved_start == resolved_declared or resolved_declared in resolved_start.parents:
            try:
                if (resolve_agent_dir(resolved_declared) / "tasks").is_dir():
                    return resolved_declared
            except (OSError, SystemExit):
                pass
    for candidate in (resolved_start, *resolved_start.parents):
        try:
            if (resolve_agent_dir(candidate) / "tasks").is_dir():
                return candidate
        except (OSError, SystemExit):
            continue
    return resolved_start


def codex_config_path(home_dir: Path | None = None) -> Path:
    """Return the global Codex config.toml path."""
    base = home_dir if home_dir is not None else Path.home()
    return base / ".codex" / "config.toml"


def enable_codex_hooks_feature(config_path: Path) -> bool:
    """Ensure [features] hooks = true exists, preserving unrelated content.

    Codex renamed the feature flag `codex_hooks` -> `hooks` (stable as of
    codex 0.141; the old `codex_hooks` is deprecated and absent from
    `codex features list`, and `plugin_hooks` was removed entirely). We write
    `hooks = true` and migrate any legacy `codex_hooks` line in the [features]
    block so upgrading installs stop riding the deprecated alias.

    Returns True when the file content changed.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text("[features]\nhooks = true\n", encoding="utf-8")
        return True

    original = config_path.read_text(encoding="utf-8")
    lines = original.splitlines()

    features_start = None
    features_end = len(lines)
    for idx, line in enumerate(lines):
        if line.strip() == "[features]":
            features_start = idx
            for j in range(idx + 1, len(lines)):
                candidate = lines[j].strip()
                if (
                    candidate.startswith("[")
                    and candidate.endswith("]")
                    and not candidate.startswith("[[")
                    and "=" not in candidate
                ):
                    features_end = j
                    break
            break

    if features_start is None:
        new_text = original
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        if new_text:
            new_text += "\n"
        new_text += "[features]\nhooks = true\n"
    else:
        updated = list(lines)
        # Within [features]: set `hooks = true`; migrate/drop legacy `codex_hooks`.
        hooks_idx = None
        legacy_idxs = []
        for idx in range(features_start + 1, features_end):
            key = updated[idx].split("=", 1)[0].strip()
            if key == "hooks":
                hooks_idx = idx
            elif key == "codex_hooks":
                legacy_idxs.append(idx)
        if hooks_idx is not None:
            updated[hooks_idx] = "hooks = true"
            for idx in sorted(legacy_idxs, reverse=True):
                del updated[idx]
        elif legacy_idxs:
            updated[legacy_idxs[0]] = "hooks = true"
            for idx in sorted(legacy_idxs[1:], reverse=True):
                del updated[idx]
        else:
            updated.insert(features_end, "hooks = true")
        new_text = "\n".join(updated)
        if original.endswith("\n"):
            new_text += "\n"

    if new_text == original:
        return False
    config_path.write_text(new_text, encoding="utf-8")
    return True


def codex_hooks_feature_enabled(config_path: Path) -> bool:
    """Read the stable Codex hooks feature without mutating user config."""
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    in_features = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_features = stripped == "[features]"
            continue
        if not in_features or "=" not in stripped or stripped.startswith("#"):
            continue
        key, value = (part.strip() for part in stripped.split("=", 1))
        if key == "hooks":
            return value.split("#", 1)[0].strip().lower() == "true"
    return False


def playbook_scripts_dir() -> Path:
    """Resolve the canonical scripts/ directory for this Playbook install."""
    here = Path(__file__).resolve()
    if here.parent.parent.name == "src":
        return here.parent.parent.parent / "scripts"
    if here.parent.parent.name == "lib" and here.parent.parent.parent.name == "scripts":
        return here.parent.parent.parent
    if here.parent.name == "provider" and (here.parent.parent / "scripts").exists():
        return here.parent.parent / "scripts"
    raise RuntimeError(f"Cannot resolve Playbook scripts directory from {here}")


def _command_for(script_name: str) -> str:
    script_path = playbook_scripts_dir() / script_name
    return f"python3 {shlex.quote(str(script_path))}"


def _playbook_hook_entry(script_name: str, matcher: str | None = None) -> dict:
    """Build a hooks.json entry. When `matcher` is given (e.g. "^apply_patch$"),
    Codex scopes the hook to tools whose name matches the regex. Omitting the
    matcher = match-all (UserPromptSubmit / Stop don't need a matcher because
    the event itself is already a single-purpose trigger).
    """
    entry: dict = {
        "hooks": [
            {
                "type": "command",
                "command": _command_for(script_name),
                "timeout": HOOK_TIMEOUT_MS,
            }
        ]
    }
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def render_playbook_hooks() -> dict:
    """Return the Playbook-owned Codex hooks.json fragment.

    PreToolUse: scoped to `^apply_patch$` only — file-edit pre-blocking. Bash
    (exec_command) is intentionally not pre-blocked; running shell commands
    without a task is allowed (matches Claude policy).

    PostToolUse: scoped to `^apply_patch$` only. A successful native file edit
    publishes gate chronology and injects the next authoritative gate. Shell
    writes remain outside pre-edit prevention because shared-worktree state
    cannot establish which session authored them.
    """
    return {
        "hooks": {
            "UserPromptSubmit": [
                _playbook_hook_entry("codex-user-prompt-hook"),
            ],
            "Stop": [
                _playbook_hook_entry("codex-stop-hook"),
            ],
            "PreToolUse": [
                _playbook_hook_entry("codex-apply-patch-hook", matcher="^apply_patch$"),
            ],
            "PostToolUse": [
                _playbook_hook_entry("codex-apply-patch-hook", matcher="^apply_patch$"),
            ],
        }
    }


def render_dispatcher_hooks(dispatcher: str = "pb-tasks") -> dict:
    """Render Codex hooks through a stable machine-level launcher name."""
    rendered = json.loads(json.dumps(render_playbook_hooks()))
    for entries in rendered["hooks"].values():
        for entry in entries:
            for hook in entry["hooks"]:
                script_name = Path(shlex.split(hook["command"])[-1]).name
                hook["command"] = shlex.join([dispatcher, "hook", script_name])
    return rendered


def _entry_commands(entry: dict) -> set[str]:
    """Return the set of `command` strings inside an entry's `hooks` array."""
    return {
        hook.get("command", "")
        for hook in entry.get("hooks", [])
        if isinstance(hook, dict)
    }


def _existing_commands_for_matcher(event_entries: list, matcher: str) -> set[str]:
    """All command strings already registered under the given matcher value."""
    seen: set[str] = set()
    for entry in event_entries:
        if not isinstance(entry, dict):
            continue
        if (entry.get("matcher") or "") != matcher:
            continue
        seen |= _entry_commands(entry)
    return seen


def merge_hooks(existing: dict, additions: dict) -> dict:
    """Merge Playbook hook entries into an existing hooks.json document.

    Dedup key is `(matcher, individual_command)` per event name (panel
    impl-review #H — codex #3): if the user has an existing entry under
    the same matcher containing the Playbook command plus a custom hook,
    re-installing must NOT add a second Playbook entry for that matcher.
    Different matchers under the same event coexist (panel finding A
    earlier — `^Bash$` linter vs `^apply_patch$` Playbook hook).
    """
    merged = json.loads(json.dumps(existing or {}))
    if not isinstance(merged.get("hooks"), dict):
        merged["hooks"] = {}
    hooks = merged["hooks"]

    for event_name, new_entries in additions.get("hooks", {}).items():
        event_entries = hooks.setdefault(event_name, [])
        for entry in new_entries:
            matcher = entry.get("matcher") or ""
            new_commands = _entry_commands(entry)
            existing_for_matcher = _existing_commands_for_matcher(event_entries, matcher)
            # Skip if every command in the new entry is already registered
            # under this matcher (idempotent re-install).
            if new_commands and new_commands.issubset(existing_for_matcher):
                continue
            event_entries.append(entry)
    return merged


_PLAYBOOK_HOOK_SCRIPTS = {
    "codex-user-prompt-hook",
    "codex-stop-hook",
    "codex-apply-patch-hook",
}


def _prune_obsolete_playbook_hooks(existing: dict, desired: dict) -> dict:
    """Drop obsolete Playbook registrations while preserving user hooks."""
    desired_keys: set[tuple[str, str, str]] = set()
    for event_name, entries in desired.get("hooks", {}).items():
        for entry in entries:
            matcher = entry.get("matcher") or ""
            for command in _entry_commands(entry):
                desired_keys.add((event_name, matcher, Path(shlex.split(command)[-1]).name))

    pruned = json.loads(json.dumps(existing or {}))
    hooks = pruned.get("hooks")
    if not isinstance(hooks, dict):
        return pruned
    for event_name, entries in list(hooks.items()):
        if not isinstance(entries, list):
            continue
        kept_entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                kept_entries.append(entry)
                continue
            matcher = entry.get("matcher") or ""
            kept_commands = []
            for hook in entry.get("hooks", []):
                if not isinstance(hook, dict):
                    kept_commands.append(hook)
                    continue
                try:
                    script = Path(shlex.split(hook.get("command", ""))[-1]).name
                except (ValueError, IndexError):
                    kept_commands.append(hook)
                    continue
                key = (event_name, matcher, script)
                if script not in _PLAYBOOK_HOOK_SCRIPTS or key in desired_keys:
                    kept_commands.append(hook)
            if kept_commands:
                kept_entries.append({**entry, "hooks": kept_commands})
        hooks[event_name] = kept_entries
    return pruned


def install_project_hooks(project_root: Path) -> Path:
    """Write or merge repo-local .codex/hooks.json for Playbook.

    Defensive against pre-existing files that are empty (`touch`-created),
    contain invalid JSON (hand-edited and broken), or have `"hooks"` set to
    null/non-dict (panel impl-review #D, gemini-3.1 #4/#5). On any of those,
    back up the broken file as `hooks.json.broken-<timestamp>` and start fresh
    rather than crashing or silently overwriting.
    """
    hooks_dir = project_root / ".codex"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hooks_path = hooks_dir / "hooks.json"

    existing: dict = {}
    if hooks_path.exists():
        text = hooks_path.read_text(encoding="utf-8").strip()
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    existing = parsed
                else:
                    raise ValueError(f"top-level JSON must be an object, got {type(parsed).__name__}")
            except (json.JSONDecodeError, ValueError) as exc:
                # Back up the broken file rather than discarding silently.
                backup_suffix = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                backup = hooks_dir / f"hooks.json.broken-{backup_suffix}"
                backup.write_text(hooks_path.read_text(encoding="utf-8"), encoding="utf-8")
                print(
                    f"[codex_hooks] {hooks_path} was unparseable ({exc}); "
                    f"backed up to {backup} and re-initializing.",
                    file=__import__("sys").stderr,
                )
                existing = {}
        # else: empty file — treat as fresh install.

    # Defend merge_hooks against `"hooks": null` from a hand-edited file.
    if not isinstance(existing.get("hooks"), dict):
        existing = {**existing, "hooks": {}}

    desired = render_playbook_hooks()
    existing = _prune_obsolete_playbook_hooks(existing, desired)
    merged = merge_hooks(existing, desired)
    hooks_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return hooks_path


def session_state_dir(project_root: Path, session_id: str) -> Path:
    """Create/validate and return this Codex session's durable state directory."""
    record, _ = ensure_session_record(
        resolve_agent_dir(project_root), "codex", session_id
    )
    return record.parent


def current_state_file(project_root: Path, session_id: str) -> Path:
    return session_state_dir(project_root, session_id) / "current_state"


def _pending_gate_edit_file(project_root: Path, session_id: str) -> Path:
    return session_state_dir(project_root, session_id) / "pending-gate-edit.json"


def has_active_task(project_root: Path, session_id: str) -> bool:
    """True iff current_state and task.md agree on this Codex owner."""
    task_file, _ = active_task_authority(project_root, session_id)
    return task_file is not None


def active_task_authority(
    project_root: Path, session_id: str
) -> tuple[Path | None, str | None]:
    """Return the claimed task or an explicit cache/authority disagreement."""
    state_file = current_state_file(project_root, session_id)
    if not state_file.exists():
        return None, None
    try:
        task_num = state_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None, f"cannot read navigation cache {state_file}"
    if not task_num:
        return None, f"navigation cache {state_file} is empty"
    from provider.session_state import SessionKey
    from tasks.core import resolve_agent_dir
    from tasks.task_document import validate_task_claim, TaskDocumentError
    try:
        task_file = validate_task_claim(
            resolve_agent_dir(project_root),
            SessionKey.from_values("codex", session_id),
            task_num,
        )
    except TaskDocumentError as exc:
        return None, str(exc)
    return task_file, None


def _runtime_root() -> Path:
    """Trusted installed checkout root supplied by the central dispatcher."""
    configured = os.environ.get("PLAYBOOK_RUNTIME_ROOT")
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def _targets_runtime(path: str, project_root: Path) -> bool:
    # The development/runtime repository may maintain itself under its own
    # active task. Protection applies when this checkout serves another
    # project, which is the standalone installation contract.
    if project_root.resolve() == _runtime_root():
        return False
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    try:
        candidate.resolve().relative_to(_runtime_root())
    except (OSError, ValueError):
        return False
    return True


def apply_patch_pre_decision(
    payload: dict,
    project_root: Path,
    session_id: str,
) -> dict | None:
    """PreToolUse decision for Codex apply_patch.

    Returns None to allow; returns {"decision": "block", "reason": "..."} to deny.
    The caller (scripts/codex-apply-patch-hook) translates a deny into
    `print(reason, file=sys.stderr); sys.exit(2)` per W0(e) decision.

    Policy mirrors Claude's structured mutation boundary:
      - With authoritative active task: allow ordinary paths and validate an
        owned task.md patch as a first-gate-only native edit.
      - Without active task: deny ONLY for code-file paths under non-management
        directories. README.md, Dockerfile, .env, etc. are allowed (Claude parity).
      - Silent-bypass guard: if patch grammar markers were seen but no paths
        could be parsed, deny with "could not parse" reason (finding 4) — a
        new/malformed patch shape must not slip through unblocked.
    """
    pending_edit = _pending_gate_edit_file(project_root, session_id)
    try:
        pending_edit.unlink(missing_ok=True)
    except OSError:
        pass
    active_task_path, authority_error = active_task_authority(project_root, session_id)
    active_task = active_task_path is not None
    if authority_error:
        return {
            "decision": "block",
            "reason": f"Playbook task authority mismatch: {authority_error}",
        }

    # Since this hook is matcher-scoped to ^apply_patch$, getting here means
    # an apply_patch tool call. A missing or non-string command field is a
    # malformed payload — defensive deny rather than silent allow (panel
    # impl-review #O). Active-task path above is unaffected.
    tool_input = payload.get("tool_input")
    command = None
    if isinstance(tool_input, dict):
        cmd = tool_input.get("command")
        if isinstance(cmd, str):
            command = cmd

    if command is None:
        if active_task:
            return None
        return {
            "decision": "block",
            "reason": (
                "could not read apply_patch payload (missing or non-string "
                "tool_input.command) — refusing without active task. "
                "Run `pb-tasks work <N>` before editing files."
            ),
        }

    parsed = parse_patch_paths(command)

    task_paths = []
    for path in parsed.paths:
        normalized = "/" + path.replace("\\", "/").lstrip("/")
        if normalized.endswith("/task.md") and "/.agent/tasks/" in normalized:
            task_paths.append(path)
    if task_paths:
        if not active_task:
            return {
                "decision": "block",
                "reason": "Task-control edits require an authoritative session claim.",
            }
        active_real = active_task_path.resolve()
        foreign = []
        for path in task_paths:
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = project_root / candidate
            if candidate.resolve() != active_real:
                foreign.append(str(candidate.resolve()))
        if foreign:
            return {
                "decision": "block",
                "reason": (
                    f"Task-control edit targets {', '.join(foreign)}, but this "
                    f"session owns {active_real}."
                ),
            }
        try:
            from tasks.gate_edit import (
                GateEditError,
                candidate_from_apply_patch,
                validate_task_candidate,
            )
            original = active_task_path.read_text(encoding="utf-8")
            candidate = candidate_from_apply_patch(
                original, command, active_task_path.as_posix()
            )
            validate_task_candidate(original, candidate)
        except (OSError, GateEditError) as exc:
            return {"decision": "block", "reason": str(exc)}
        pending_edit.write_text(
            json.dumps(
                {
                    "task_path": active_task_path.relative_to(project_root).as_posix(),
                    "original": original,
                }
            ),
            encoding="utf-8",
        )
        return None

    runtime_paths = [p for p in parsed.paths if _targets_runtime(p, project_root)]
    if runtime_paths:
        return {
            "decision": "block",
            "reason": (
                "the installed Playbook runtime is read-only during agent work: "
                + ", ".join(runtime_paths)
            ),
        }

    if active_task:
        return None

    # Not an apply_patch attempt at all (no grammar markers) — allow.
    if not parsed.had_headers:
        return None

    # Grammar markers present but no paths extracted — silent-bypass guard.
    if not parsed.paths:
        return {
            "decision": "block",
            "reason": (
                "could not parse apply_patch body — refusing without active task. "
                "Run `pb-tasks work <N>` before editing files."
            ),
        }

    # Filter: keep only code-file paths that are NOT under .agent/ or .claude/.
    code_paths = [
        p for p in parsed.paths
        if _is_code_file_path(p) and not _is_management_path(p)
    ]

    if not code_paths:
        # All paths are management dirs or non-code (e.g. README.md, Dockerfile).
        # Claude-parity: allowed without an active task.
        return None

    listed = ", ".join(code_paths)
    return {
        "decision": "block",
        "reason": (
            f"no active task — run `pb-tasks work <N>` before editing "
            f"code: {listed}"
        ),
    }


_GATE_LINE_RE = re.compile(r"^[ \t]*- \[( |x|X)\]\s*(.*)$")

# Freehand-mode trigger. Matches gate text starting with "Freehand" — covers
# bare "Freehand", "Freehand — work is done", "Freehand debrief — ...", and
# other discussion-style variants observed in real task.md files. The single
# exception is "Freehand log" (cleanup gate from `pb-tasks freehand` workflow at
# cli.py:1620), which must remain a normal blocking gate.
# Stays in lockstep with the bash case patterns in scripts/state-echo-hook
# and scripts/stop-hook.
_FREEHAND_RE = re.compile(r"^Freehand(?! log\b)")


def _read_active_task_number(project_root: Path, session_id: str) -> str | None:
    """Return the active task number (string) from current_state, or None."""
    state_file = current_state_file(project_root, session_id)
    if not state_file.exists():
        return None
    try:
        text = state_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _find_task_file(project_root: Path, task_num: str) -> Path | None:
    """Locate `.agent/tasks/<task_num>-*/task.md` for the given task number."""
    tasks_dir = resolve_agent_dir(project_root) / "tasks"
    if not tasks_dir.exists():
        return None
    prefix = f"{task_num}-"
    for child in tasks_dir.iterdir():
        if child.is_dir() and child.name.startswith(prefix):
            task_file = child / "task.md"
            if task_file.exists():
                return task_file
    return None


def _scan_gates(task_file: Path) -> tuple[int, int, str | None, int | None]:
    """Return (done_count, total_count, first_unchecked_text, line_number).

    first_unchecked_text is None if all gates are done.
    """
    try:
        lines = task_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0, 0, None, None

    total = 0
    done = 0
    first_unchecked: str | None = None
    first_unchecked_line: int | None = None
    for line_number, line in enumerate(lines, start=1):
        m = _GATE_LINE_RE.match(line)
        if not m:
            continue
        total += 1
        marker = m.group(1)
        if marker.lower() == "x":
            done += 1
        elif first_unchecked is None:
            first_unchecked = m.group(2).strip()
            first_unchecked_line = line_number
    return done, total, first_unchecked, first_unchecked_line


def _is_monitor_board(task_file: Path) -> bool:
    try:
        from tasks.task_document import TaskDocument

        return TaskDocument.parse(task_file.read_text(encoding="utf-8")).is_monitor_board
    except (OSError, ValueError):
        return False


def _format_gate_echo(
    task_num: str,
    done: int,
    total: int,
    gate_text: str | None,
    *,
    task_path: str | None = None,
    gate_line: int | None = None,
) -> str:
    """Unicode-safe bounded mirror of gate-echo-lib.sh ``format_context``."""
    # Distinguish a stub task (zero gate lines) from a fully-completed task.
    # Without this branch, total=0 falls through to "all gates done" which is
    # actively misleading and can trigger session-end actions (impl-review #2).
    if total == 0:
        return f"# [{task_num}] no gates defined yet — add work plan before continuing."
    if gate_text is None:
        return f"# [{task_num}] — all gates done. Stay for follow-up. Auto-closes on task switch."
    # Freehand-mode echo when gate text is "Freehand" (bare) or starts with
    # "Freehand <punctuation>..." (e.g. "Freehand — work is done"). Must NOT
    # match "Freehand log" — alphanumeric continuations are normal gates
    # (cli.py:1620 cleanup gate). Pattern stays in lockstep with bash sites.
    if _FREEHAND_RE.match(gate_text or ""):
        return f"# [{task_num}] Freehand mode — wait for user instructions. Close only when user says done."
    prefix = f"# Working on task [{task_num}] gate ({done}/{total}) -> [ ] "
    route_path = task_path or f".agent/tasks/{task_num}-*/task.md"
    route_line = str(gate_line) if gate_line is not None else "?"
    route = f"\n# Full gate: {route_path}:{route_line}"
    available = max(1, 520 - len(prefix) - len(route))
    bounded_gate = gate_text
    if len(bounded_gate) > available:
        bounded_gate = bounded_gate[: max(0, available - 1)] + "…"
    return prefix + bounded_gate + route


def _no_active_task_echo() -> str:
    return "# No active task (pb-tasks work <N> to activate)"


def apply_patch_post_context(
    payload: dict,
    project_root: Path,
    session_id: str,
) -> dict:
    """Publish a native task edit and build its next-gate context.

    Emits the same bounded first-unchecked-gate echo Claude's
    `state-echo-hook` produces and records one chronology event when the patch
    closed the current gate. The installed matcher limits this handler to
    `apply_patch`; shell test results are evidence for the unchanged gate, not
    task-state transitions that need another pointer injection.

    Return shape matches Codex's `hookSpecificOutput.additionalContext` contract;
    text is injected as a developer-role message in the next turn (verified in W0(d)).
    """
    pending_edit = _pending_gate_edit_file(project_root, session_id)
    if payload.get("tool_name") == "apply_patch" and pending_edit.exists():
        try:
            pending = json.loads(pending_edit.read_text(encoding="utf-8"))
            relative = pending["task_path"]
            original = pending["original"]
            task_path = project_root / relative
            candidate = task_path.read_text(encoding="utf-8")
            from tasks.gate_edit import gate_closure_from_documents
            closure = gate_closure_from_documents(original, candidate)
            if closure is not None:
                from tasks.chat_state import (
                    append_chat_event,
                    chat_timestamp,
                    derive_event_key,
                )
                task_num_for_event = task_path.parent.name.split("-", 1)[0]
                marker = f"G{task_num_for_event}:{closure.line + 1}"
                append_chat_event(
                    resolve_agent_dir(project_root),
                    marker,
                    "codex",
                    session_id,
                    chat_timestamp(),
                    f"- [x] {closure.after}",
                    event_key=derive_event_key(
                        marker, "codex", session_id, closure.before, closure.after
                    ),
                )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            # Feedback/chronology are post-tool projections. The authoritative
            # task edit already happened; never misrepresent projection failure
            # as mutation failure.
            pass
        finally:
            try:
                pending_edit.unlink(missing_ok=True)
            except OSError:
                pass

    task_num = _read_active_task_number(project_root, session_id)

    if task_num is None:
        context = _no_active_task_echo()
    else:
        task_file = _find_task_file(project_root, task_num)
        if task_file is None:
            context = _no_active_task_echo()
        else:
            done, total, first_unchecked, gate_line = _scan_gates(task_file)
            if _is_monitor_board(task_file):
                context = (
                    f"# [{task_num}] Monitor board — reconcile user intent, "
                    f"incoming work, and lane events in task.md ({total - done} open gate(s))."
                )
            else:
                context = _format_gate_echo(
                    task_num,
                    done,
                    total,
                    first_unchecked,
                    task_path=task_file.relative_to(project_root).as_posix(),
                    gate_line=gate_line,
                )

    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    }


def _session_counter_path(project_root: Path, session_id: str) -> Path:
    return session_state_dir(project_root, session_id) / "counters"


def _agent_dir_writable(project_root: Path) -> bool:
    agent_dir = resolve_agent_dir(project_root)
    return agent_dir.is_dir() and agent_dir.exists() and os.access(agent_dir, os.W_OK)


def _normalize_prompt(prompt: str) -> str:
    text = prompt.replace("\n", " ")
    text = re.sub(r" +", " ", text)
    text = re.sub(r"<ide_opened_file>[^<]*</ide_opened_file>", "", text)
    text = re.sub(r"<ide_selection>[^<]*</ide_selection>", "", text)
    text = text.strip()

    max_len = 500
    if len(text) > max_len:
        removed = len(text) - max_len
        text = f"{text[:max_len]}...[{removed} chars removed]"
    return text


def reset_session_counters(project_root: Path, session_id: str) -> Path:
    counter_path = _session_counter_path(project_root, session_id)
    counter_path.parent.mkdir(parents=True, exist_ok=True)

    preserved: list[str] = []
    if counter_path.exists():
        for line in counter_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("gate_"):
                preserved.append(line)

    lines = ["tools=0", "writes=0", *preserved]
    counter_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return counter_path


def append_prompt_to_chat_log(
    project_root: Path,
    session_id: str,
    prompt: str | None,
    *,
    timestamp: dt.datetime | None = None,
) -> bool:
    """Append a Codex UserPromptSubmit prompt to .agent/chat_log.md.

    Returns True when a non-empty prompt was logged, False when logging was
    intentionally skipped (e.g. empty prompt or non-writable .agent/).
    """
    if not _agent_dir_writable(project_root):
        return False

    user_message = _normalize_prompt(prompt or "")
    if not user_message:
        return False

    ts = (timestamp or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d %H:%M:%S UTC")
    from tasks.chat_state import append_chat_message
    append_chat_message(
        resolve_agent_dir(project_root), "codex", session_id, ts, user_message
    )

    reset_session_counters(project_root, session_id)
    return True


def _active_task_stop_decision(project_root: Path, session_id: str) -> dict:
    """Reuse the existing authoritative stop guard for active-task sessions."""
    stop_hook = playbook_scripts_dir() / "stop-hook"
    env = os.environ.copy()
    for name in ("CLAUDE_CODE_SESSION_ID", "ANTIGRAVITY_CONVERSATION_ID",
                 "PLAYBOOK_BRIDGE_PROVIDER"):
        env.pop(name, None)
    env["PLAYBOOK_SESSION_ID"] = session_id
    env["PLAYBOOK_PROVIDER"] = "codex"
    env["CODEX_THREAD_ID"] = session_id
    try:
        result = subprocess.run(
            ["bash", str(stop_hook)],
            cwd=project_root,
            input=json.dumps({"session_id": session_id}),
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
    except OSError as exc:
        return {
            "decision": "block",
            "reason": f"Playbook stop guard failed to run: {exc}",
        }

    if result.returncode == 0:
        return {}
    reason = (result.stderr or result.stdout or "Complete all gates before finishing.").strip()
    return {
        "decision": "block",
        "reason": reason,
    }


def codex_stop_decision(project_root: Path, session_id: str) -> dict:
    """Enforce attributable task lifecycle state at Codex Stop.

    Repository diffs are deliberately not inspected here: a shared worktree
    cannot tell which session authored a change. No-task edits through
    ``apply_patch`` are rejected before mutation by the attributable tool hook;
    shell writes remain outside Playbook's enforcement boundary.
    """
    active_task, authority_error = active_task_authority(project_root, session_id)
    if authority_error:
        return {
            "decision": "block",
            "reason": f"Playbook task authority mismatch: {authority_error}",
        }
    if active_task is not None:
        return _active_task_stop_decision(project_root, session_id)
    return {}
