"""
Helpers for Codex lifecycle hook installation and runtime decisions.

Codex hook execution currently lives outside the provider policy stubs: Codex
invokes commands declared in hooks.json directly. This module keeps the logic
pure/testable while the small scripts in scripts/ act as thin entrypoints.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from .policy import _is_code_file_path, _is_management_path
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
MISSING_FILE_DIGEST = "__MISSING__"
SESSION_BASELINE_KEY = "__session__"
_CHAT_LOG_HEADER = "# Project Chat Log\n\nUser messages logged with timestamps.\n\n"
_OLD_CHAT_HEADER_RE = re.compile(r"^\*\*\[([0-9-]{10} [0-9:]{8} UTC)\]\*\*(.*)$")
_NEW_CHAT_HEADER_RE = re.compile(r"^\*\*\[M(\d{3,})\]\*\* ")


def resolve_session_id() -> str:
    """Use the same session resolver as pb-tasks."""
    return _resolve_shared_session_id()


def find_project_root(start: Path) -> Path:
    """Find legacy or multi-user Playbook state from a nested Codex cwd."""
    for candidate in (start.resolve(), *start.resolve().parents):
        try:
            if (resolve_agent_dir(candidate) / "tasks").is_dir():
                return candidate
        except (OSError, SystemExit):
            continue
    return start.resolve()


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

    PostToolUse: scoped to `^apply_patch$` AND `^exec_command$` so the gate
    echo fires on both file edits and bash. The same `codex-apply-patch-hook`
    serves both — `apply_patch_post_context` emits gate-echo text without
    parsing patch contents, so it works for any tool type. T134 closes the
    pre-existing gap (MIND_MAP [44]: "Bash shell-out bypasses the apply_patch
    matcher entirely — pre-edit prevention is apply_patch-only, Stop hook still
    catches at turn boundary"). With this matcher pair, gate echo now also
    fires on every bash invocation during an active task.
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
                _playbook_hook_entry("codex-apply-patch-hook", matcher="^exec_command$"),
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

    merged = merge_hooks(existing, render_playbook_hooks())
    hooks_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return hooks_path


def current_state_file(project_root: Path, session_id: str) -> Path:
    return resolve_agent_dir(project_root) / "sessions" / session_id / "current_state"


def has_active_task(project_root: Path, session_id: str) -> bool:
    """True iff current_state names a task and its task.md exists.

    The task.md existence check (panel impl-review #J) prevents a split-brain
    where current_state points at a task whose directory was deleted —
    apply_patch_pre_decision would allow but apply_patch_post_context would
    say "no active task", giving the model contradictory signals.
    """
    state_file = current_state_file(project_root, session_id)
    if not state_file.exists():
        return False
    try:
        task_num = state_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not task_num:
        return False
    return _find_task_file(project_root, task_num) is not None


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

    Policy mirrors Claude's `evaluate_tool_call`:
      - With active task: always allow.
      - Without active task: deny ONLY for code-file paths under non-management
        directories. README.md, Dockerfile, .env, etc. are allowed (Claude parity).
      - Silent-bypass guard: if patch grammar markers were seen but no paths
        could be parsed, deny with "could not parse" reason (finding 4) — a
        new/malformed patch shape must not slip through unblocked.
    """
    active_task = has_active_task(project_root, session_id)

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


def _scan_gates(task_file: Path) -> tuple[int, int, str | None]:
    """Return (done_count, total_count, first_unchecked_text).

    first_unchecked_text is None if all gates are done.
    """
    try:
        lines = task_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0, 0, None

    total = 0
    done = 0
    first_unchecked: str | None = None
    for line in lines:
        m = _GATE_LINE_RE.match(line)
        if not m:
            continue
        total += 1
        marker = m.group(1)
        if marker.lower() == "x":
            done += 1
        elif first_unchecked is None:
            first_unchecked = m.group(2).strip()
    return done, total, first_unchecked


def _format_gate_echo(task_num: str, done: int, total: int, gate_text: str | None) -> str:
    """Mirror of gate-echo-lib.sh `format_context` (minus the file:line suffix).

    The bash version appends `# Done? Check the box: <rel_path>:<line>`. We omit
    that for Codex because the agent already has full repo access and the
    relative-path hint is noise in apply_patch's transcript context.
    """
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
    return f"# Working on task [{task_num}] gate ({done}/{total}) -> [ ] {gate_text}"


def _no_active_task_echo() -> str:
    return "# No active task (pb-tasks work <N> to activate)"


def apply_patch_post_context(
    payload: dict,
    project_root: Path,
    session_id: str,
) -> dict:
    """PostToolUse handler for Codex apply_patch.

    Emits the same first-unchecked-gate echo Claude's `state-echo-hook` produces
    (W3 minimum-viable scope: gate text + no-task / all-done fallback + freehand
    suppression). Stateful behaviors (counters, transition logs, typed nudges,
    write log) are parked — see task 123 W3 / parked items.

    Return shape matches Codex's `hookSpecificOutput.additionalContext` contract;
    text is injected as a developer-role message in the next turn (verified in W0(d)).
    """
    task_num = _read_active_task_number(project_root, session_id)

    if task_num is None:
        context = _no_active_task_echo()
    else:
        task_file = _find_task_file(project_root, task_num)
        if task_file is None:
            context = _no_active_task_echo()
        else:
            done, total, first_unchecked = _scan_gates(task_file)
            context = _format_gate_echo(task_num, done, total, first_unchecked)

    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    }


def _baseline_key(turn_id: str | None) -> str:
    if not turn_id:
        return SESSION_BASELINE_KEY
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in turn_id)


def _turn_baseline_file(project_root: Path, session_id: str, turn_id: str | None) -> Path:
    safe_turn_id = _baseline_key(turn_id)
    session_dir = resolve_agent_dir(project_root) / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir / f"codex-dirty-baseline-{safe_turn_id}.json"


def _stop_block_marker_file(project_root: Path, session_id: str, turn_id: str | None) -> Path:
    """Marker recording that the Codex Stop hook already blocked once this turn.

    Codex has NO stop-block cap — unlike Claude's `stop_hook_active`
    self-release and the `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` runtime cap (2.1.143).
    So a persistent block condition loops forever: e.g. a concurrent agent
    editing the shared worktree, whose changes whole-tree `git status` can't
    distinguish from this session's. We block at most once per turn, then
    self-release. The marker is keyed by turn_id and reset on each new turn's
    baseline so a genuinely new edit still gets nudged once.
    """
    safe_turn_id = _baseline_key(turn_id)
    session_dir = resolve_agent_dir(project_root) / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir / f"codex-stop-blocked-{safe_turn_id}.flag"


def _chat_log_path(project_root: Path) -> Path:
    return resolve_agent_dir(project_root) / "chat_log.md"


def _chat_counter_path(project_root: Path) -> Path:
    return resolve_agent_dir(project_root) / "chat_log_counter"


def _session_counter_path(project_root: Path, session_id: str) -> Path:
    return resolve_agent_dir(project_root) / "sessions" / session_id / "counters"


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


def _migrate_chat_log_if_needed(log_path: Path, counter_path: Path) -> None:
    if not log_path.exists():
        return
    original = log_path.read_text(encoding="utf-8")
    if not original.strip():
        return
    if any(_NEW_CHAT_HEADER_RE.match(line) for line in original.splitlines()):
        return
    if not any(_OLD_CHAT_HEADER_RE.match(line) for line in original.splitlines()):
        return

    msg_num = 0
    new_lines: list[str] = []
    for line in original.splitlines():
        match = _OLD_CHAT_HEADER_RE.match(line)
        if match:
            msg_num += 1
            suffix = match.group(2)
            new_lines.append(f"**[M{msg_num:03d}]** [{match.group(1)}]{suffix}")
        else:
            new_lines.append(line)
    new_text = "\n".join(new_lines)
    if original.endswith("\n"):
        new_text += "\n"
    log_path.write_text(new_text, encoding="utf-8")
    counter_path.write_text(f"{msg_num}\n", encoding="utf-8")


def _current_chat_counter(log_path: Path, counter_path: Path) -> int:
    if counter_path.exists():
        try:
            return int(counter_path.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            pass

    highest = 0
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            match = _NEW_CHAT_HEADER_RE.match(line)
            if match:
                highest = max(highest, int(match.group(1)))
    return highest


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

    log_path = _chat_log_path(project_root)
    counter_path = _chat_counter_path(project_root)

    if not log_path.exists():
        log_path.write_text(_CHAT_LOG_HEADER, encoding="utf-8")

    _migrate_chat_log_if_needed(log_path, counter_path)

    ts = (timestamp or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d %H:%M:%S UTC")
    current = _current_chat_counter(log_path, counter_path)
    next_id = current + 1
    counter_path.write_text(f"{next_id}\n", encoding="utf-8")

    with log_path.open("a", encoding="utf-8") as f:
        f.write("---\n\n")
        f.write(f"**[M{next_id:03d}]** [{ts}] `HOST` (codex/{session_id})\n\n")
        f.write(f"{user_message}\n\n")

    reset_session_counters(project_root, session_id)
    return True


def _digest_for_file(path: Path) -> str:
    if not path.exists():
        return MISSING_FILE_DIGEST
    if path.is_dir():
        return MISSING_FILE_DIGEST
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _all_code_files_state(project_root: Path) -> dict[str, str]:
    """Fallback snapshot for non-Git projects: hash all code files under the repo."""
    state: dict[str, str] = {}
    skip_dirs = {".git", ".agent", ".claude", ".codex", ".pytest_cache", ".hypothesis", "__pycache__"}
    for path in project_root.rglob("*"):
        if not path.is_file():
            continue
        rel_path = path.relative_to(project_root).as_posix()
        parts = set(rel_path.split("/"))
        if parts & skip_dirs:
            continue
        if not _is_code_file_path(rel_path):
            continue
        state[rel_path] = _digest_for_file(path)
    return state


def code_state(project_root: Path) -> dict[str, str]:
    """Return a snapshot of relevant code changes for the current project.

    In Git repos, only dirty code files are tracked. Outside Git, fall back to
    a full code-file snapshot so no-task steering still works.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "-z",
            ],
            capture_output=True,
            check=False,
        )
    except OSError:
        return _all_code_files_state(project_root)

    if result.returncode != 0:
        return _all_code_files_state(project_root)

    state: dict[str, str] = {}
    entries = result.stdout.decode("utf-8", errors="replace").split("\0")
    idx = 0
    while idx < len(entries):
        entry = entries[idx]
        idx += 1
        if not entry:
            continue
        if len(entry) < 4:
            continue
        status = entry[:2]
        rel_path = entry[3:]
        if "R" in status or "C" in status:
            if idx >= len(entries):
                break
            rel_path = entries[idx]
            idx += 1
        rel_path = rel_path.strip()
        if not rel_path or not _is_code_file_path(rel_path):
            continue
        state[rel_path] = _digest_for_file(project_root / rel_path)
    return state


def save_turn_baseline(project_root: Path, session_id: str, turn_id: str | None) -> Path:
    """Persist the starting dirty code state for a Codex turn.

    If turn_id is unavailable, fall back to a session-scoped baseline key.
    """
    baseline_file = _turn_baseline_file(project_root, session_id, turn_id)
    baseline_file.write_text(
        json.dumps(code_state(project_root), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Re-arm the per-turn stop-block self-release: a fresh turn may legitimately
    # block once again. (Keyed by turn_id, so distinct turns are independent;
    # this matters when turn_id is missing and degrades to a session key.)
    marker = _stop_block_marker_file(project_root, session_id, turn_id)
    try:
        marker.unlink()
    except (OSError, FileNotFoundError):
        pass
    return baseline_file


def load_turn_baseline(project_root: Path, session_id: str, turn_id: str | None) -> dict[str, str] | None:
    baseline_file = _turn_baseline_file(project_root, session_id, turn_id)
    if not baseline_file.exists():
        return None
    try:
        return json.loads(baseline_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def has_new_code_changes(baseline: dict[str, str], current: dict[str, str]) -> bool:
    """Return True when the current dirty-code snapshot differs from the baseline."""
    for path, digest in current.items():
        if path not in baseline:
            return True
        if baseline[path] != digest:
            return True
    return False


def _active_task_stop_decision(project_root: Path, session_id: str) -> dict:
    """Reuse the existing authoritative stop guard for active-task sessions."""
    stop_hook = playbook_scripts_dir() / "stop-hook"
    env = os.environ.copy()
    env["PLAYBOOK_SESSION_ID"] = session_id
    try:
        result = subprocess.run(
            ["bash", str(stop_hook)],
            cwd=project_root,
            input=json.dumps({}),
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


def stop_decision_for_no_task_code_changes(
    project_root: Path,
    session_id: str,
    turn_id: str | None,
) -> dict:
    """Return the JSON response for Codex Stop hooks, capped at one block per turn.

    Missing turn identifiers degrade to a session-scoped baseline rather than
    disabling enforcement silently. **Self-release**: any block decision fires at
    most once per turn — Codex has no stop-block cap (no `stop_hook_active`,
    no `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`), so a persistent block condition (e.g. a
    concurrent agent editing the shared worktree) would otherwise loop forever.
    The marker is reset each new turn (`save_turn_baseline`), so a genuinely new
    edit still gets nudged once. See `_stop_block_marker_file`.
    """
    decision = _compute_stop_decision(project_root, session_id, turn_id)
    if decision.get("decision") == "block":
        marker = _stop_block_marker_file(project_root, session_id, turn_id)
        if marker.exists():
            return {}  # already nudged this turn — let the turn end
        try:
            marker.write_text("1", encoding="utf-8")
        except OSError:
            pass
    return decision


def _compute_stop_decision(
    project_root: Path,
    session_id: str,
    turn_id: str | None,
) -> dict:
    """Raw Codex stop evaluation (no self-release; wrapped by
    stop_decision_for_no_task_code_changes)."""
    if has_active_task(project_root, session_id):
        return _active_task_stop_decision(project_root, session_id)

    baseline = load_turn_baseline(project_root, session_id, turn_id)
    if baseline is None:
        return {}

    current = code_state(project_root)
    if not has_new_code_changes(baseline, current):
        return {}

    changed = sorted(path for path, digest in current.items() if baseline.get(path) != digest)
    changed_preview = ", ".join(changed[:3])
    if len(changed) > 3:
        changed_preview += ", ..."
    reason = (
        "You changed code without an active Playbook task"
        + (f" ({changed_preview})" if changed_preview else "")
        + ". Run `pb-tasks work <N>` if this belongs to an existing task, "
          "or create one with `pb-tasks new quick <name> <intent>` before continuing."
    )
    return {
        "decision": "block",
        "reason": reason,
    }
