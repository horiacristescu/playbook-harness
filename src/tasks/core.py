"""Task management operations for .agent/tasks/ directories."""
from __future__ import annotations

import functools
import os
import re
import subprocess
from pathlib import Path

VERSION = "1.3.5"

AGENT_PROCESS_NAMES = frozenset({"claude", "codex", "agy", "pi"})


@functools.lru_cache(maxsize=1)
def find_agent_root_pid() -> int | None:
    """Walk parent process tree, return PID of the highest agent ancestor.

    Identifies claude/codex/agy/pi processes by `comm` (basename, no args).
    Returns None if no agent found within 20 hops or if `ps` is unavailable.
    Used as fallback when PLAYBOOK_SESSION_ID env var isn't propagated —
    Python and bash both walk the same tree and converge on the same PID.
    Result is cached: process tree is stable for the lifetime of this process.
    """
    pid = os.getppid()
    last_agent_pid: int | None = None
    for _ in range(20):
        if pid in (0, 1):
            break
        try:
            r = subprocess.run(
                ["ps", "-p", str(pid), "-o", "ppid=,comm="],
                capture_output=True, text=True, timeout=1,
            )
        except (OSError, subprocess.TimeoutExpired):
            break
        if r.returncode != 0 or not r.stdout.strip():
            break
        parts = r.stdout.strip().split(None, 1)
        if len(parts) < 2:
            break
        try:
            ppid = int(parts[0])
        except ValueError:
            break
        comm = os.path.basename(parts[1].strip())
        if comm in AGENT_PROCESS_NAMES:
            last_agent_pid = pid
        if ppid == pid:
            break
        pid = ppid
    return last_agent_pid


def resolve_session_id() -> str:
    """Resolve session_id used to namespace .agent/sessions/<id>/.

    Order: PLAYBOOK_SESSION_ID env → native Codex thread ID → ancestor scan
    (root agent PID) → immediate-parent PID. The ancestor scan is the robust path: it survives
    env-propagation failures (VSCode CLAUDE_ENV_FILE quirks, missing wrappers,
    subprocess loss). Bash hooks mirror this resolver in gate-echo-lib.sh.
    """
    sid = os.environ.get("PLAYBOOK_SESSION_ID", "")
    if sid:
        return sid
    codex_thread = os.environ.get("CODEX_THREAD_ID", "")
    if codex_thread:
        return codex_thread
    agent_pid = find_agent_root_pid()
    if agent_pid is not None:
        return f"pid-{agent_pid}"
    return f"pid-{os.getppid()}"

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def _validate_username(name: str) -> None:
    """Raise SystemExit if name is not a safe directory component."""
    if not name or name in (".", "..") or not _USERNAME_RE.match(name) or "/" in name:
        print(
            f"Error: .agent/current_user contains invalid username {name!r}.\n"
            "Must be non-empty, start with a letter or digit, and contain only "
            "letters, digits, hyphens, underscores, and dots (no spaces or slashes).",
            file=__import__("sys").stderr,
        )
        raise SystemExit(1)


def resolve_agent_dir(project_path: Path) -> Path:
    """Return the agent state root for this project.

    Multi-user mode: .agent/current_user exists → .agent/<username>/
    Legacy mode:     .agent/current_user absent  → .agent/  (unchanged)
    Invalid content: print error and exit(1).
    """
    agent_root = project_path / ".agent"
    marker = agent_root / "current_user"
    if agent_root.is_symlink():
        print(
            f"Error: project agent directory may not be a symlink: {agent_root}",
            file=__import__("sys").stderr,
        )
        raise SystemExit(1)
    if marker.is_symlink():
        print(
            f"Error: .agent/current_user may not be a symlink: {marker}",
            file=__import__("sys").stderr,
        )
        raise SystemExit(1)
    if not marker.exists():
        return agent_root
    name = marker.read_text(encoding="utf-8").strip()
    _validate_username(name)
    selected = agent_root / name
    if selected.is_symlink():
        print(
            f"Error: selected agent directory may not be a symlink: {selected}",
            file=__import__("sys").stderr,
        )
        raise SystemExit(1)
    return selected


# Task type → pattern name in playbook skill
PLAYBOOKS = {
    "feature": "Build",
    "build": "Build",
    "bugfix": "Fix",
    "refactor": "Build",
    "cleanup": "Fix",
    "ops": "Build",
    "audit": "Evaluate",
    "eval": "Evaluate",
    "research": "Investigate",
}



def _slugify(name: str) -> str:
    """Convert name to lowercase hyphen-separated slug."""
    slug = re.sub(r'[\s_]+', '-', name)
    slug = re.sub(r'[^a-zA-Z0-9-]', '', slug)
    slug = re.sub(r'-+', '-', slug)
    return slug.strip('-').lower()


def _display_title(name: str) -> str:
    """Render a task name for markdown headers."""
    return name.replace("-", " ").replace("_", " ").title()


def _next_task_number(tasks_dir: Path) -> int:
    """Find the next available task number."""
    if not tasks_dir.exists():
        return 1

    max_num = 0
    for item in tasks_dir.iterdir():
        if item.is_dir():
            match = re.match(r'^(\d+)-', item.name)
            if match:
                num = int(match.group(1))
                max_num = max(max_num, num)

    return max_num + 1



def _find_playbook_skill(project_path: Path | None = None) -> Path | None:
    """Find the playbook SKILL.md file.

    Resolution order:
    1. project_path/.claude/skills/playbook/SKILL.md  (project-local)
    2. ~/.claude/skills/playbook/SKILL.md              (home install)
    """
    if project_path:
        skill = project_path / ".claude" / "skills" / "playbook" / "SKILL.md"
        if skill.exists():
            return skill

    home_skill = Path.home() / ".claude" / "skills" / "playbook" / "SKILL.md"
    if home_skill.exists():
        return home_skill

    return None


def _load_playbook(task_type: str, project_path: Path | None = None) -> str | None:
    """Load a pattern template from the unified playbook skill.

    Extracts the ```markdown block under the matching ### Pattern heading.
    Returns the template text, or None if not found.
    """
    pattern_name = PLAYBOOKS.get(task_type)
    if not pattern_name:
        return None

    skill_path = _find_playbook_skill(project_path)
    if not skill_path:
        return None

    content = skill_path.read_text(encoding="utf-8")

    # Extract the ```markdown ... ``` block under ### <pattern_name>
    in_section = False
    in_code_block = False
    template_lines = []

    for line in content.splitlines():
        if line.strip() == f"### {pattern_name}":
            in_section = True
            continue
        if in_section:
            # Stop at next ### heading
            if line.startswith("### ") and not in_code_block:
                break
            if line.strip() == "```markdown":
                in_code_block = True
                continue
            if in_code_block:
                if line.strip() == "```":
                    break
                template_lines.append(line)

    return "\n".join(template_lines) if template_lines else None


def _find_custom_playbook(project_path: Path, task_type: str) -> Path | None:
    """Check if a custom playbook template exists in .agent/playbooks/."""
    playbook = resolve_agent_dir(project_path) / "playbooks" / f"{task_type}.md"
    return playbook if playbook.exists() else None


def list_all_types(project_path: Path) -> list[str]:
    """Return sorted list of all available task types (built-in + custom)."""
    types = set(PLAYBOOKS.keys()) | {"quick"}
    playbooks_dir = resolve_agent_dir(project_path) / "playbooks"
    if playbooks_dir.exists():
        for f in playbooks_dir.glob("*.md"):
            if f.name != "README.md":
                types.add(f.stem)
    return sorted(types)


def create_task(project_path: Path, name: str, task_type: str | None = None,
                intent_text: str | None = None, stub: bool = False) -> Path:
    """Create a new task with the given name.

    Args:
        project_path: Path to the project root
        name: Human-readable name for the task
        task_type: Task type (feature, bugfix, etc.) for playbook template.
            If a matching .agent/playbooks/<type>.md exists, uses that
            instead of the base Python template.
        intent_text: Optional intent paragraph to pre-fill ## Intent section.
        stub: If True, generate minimal stub (no gates) instead of full template.

    Returns:
        Path to the created task.md file
    """
    tasks_dir = resolve_agent_dir(project_path) / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    task_num = _next_task_number(tasks_dir)
    slug = _slugify(name)
    folder_name = f"{task_num:03d}-{slug}"

    task_dir = tasks_dir / folder_name
    task_dir.mkdir()

    # Check for custom playbook template first
    custom = _find_custom_playbook(project_path, task_type) if task_type else None

    if stub:
        # Stub mode: minimal template with no gates
        from tasks.template import render_stub_template
        content = render_stub_template(
            num=task_num, title=_display_title(name),
            intent_text=intent_text or "",
            task_type=task_type,
        )
    elif custom:
        content = custom.read_text(encoding="utf-8")
        content = content.replace("{{NNN}}", f"{task_num:03d}")
        content = content.replace("{{TITLE}}", _display_title(name))
    else:
        # Fall back to base Python template
        from tasks.template import render_template
        content = render_template(num=task_num, title=_display_title(name), task_type=task_type)

        # Append playbook template if task_type specified
        if task_type:
            role_template = _load_playbook(task_type, project_path)
            if role_template:
                content += "\n" + role_template + "\n"

    # Pre-fill Intent section if intent_text provided
    if intent_text and not stub:
        # Replace placeholder in all template variants
        for placeholder in [
            "(what we want to achieve \u2014 the outcome, not the activity)",
            "(one line \u2014 what to do and how to verify)",
        ]:
            if placeholder in content:
                content = content.replace(placeholder, intent_text)
                break

    task_file = task_dir / "task.md"
    task_file.write_text(content, encoding="utf-8")

    return task_file


def _extract_status(task_file: Path) -> str:
    """Extract status from task file (line after last ## Status)."""
    try:
        lines = task_file.read_text(encoding="utf-8").splitlines()
        status_idx = None
        for i, line in enumerate(lines):
            if line.strip() == "## Status":
                status_idx = i
        if status_idx is not None and status_idx + 1 < len(lines):
            return lines[status_idx + 1].strip()
        return "unknown"
    except Exception:
        return "error"


def _extract_problem(task_file: Path) -> str:
    """Extract first line of Problem/Intent section from task file."""
    try:
        lines = task_file.read_text(encoding="utf-8").splitlines()
        in_section = False
        for line in lines:
            if line.strip() in ("## Problem", "## Intent"):
                in_section = True
                continue
            if in_section:
                if not line.strip():
                    continue
                if line.startswith("##"):
                    break
                text = line.strip()
                if text.startswith("(") and text.endswith(")"):
                    text = text[1:-1]
                return text
        return ""
    except Exception:
        return ""


def _extract_head_position(task_file: Path) -> str:
    """Find the first unchecked checkbox or empty required field."""
    try:
        lines = task_file.read_text(encoding="utf-8").splitlines()
        for line in lines:
            stripped = line.strip()
            # Unchecked checkbox
            if stripped.startswith("- [ ]"):
                return stripped[6:].strip()  # text after "- [ ] "
            # Empty required field (line ending with : and nothing after)
            if stripped.endswith(":") and stripped.startswith("- **"):
                return stripped
        return "(all gates checked)"
    except Exception:
        return "(error reading)"


def _is_done(task_file: Path) -> bool:
    """Check if a task's status starts with 'done'."""
    return _extract_status(task_file).startswith("done")


def _find_active_task(project_path: Path, name_filter: str = "") -> Path | None:
    """Find the active task: earliest non-done task with unchecked gates.

    If name_filter is given, only match tasks whose folder name contains it.
    """
    tasks_dir = resolve_agent_dir(project_path) / "tasks"
    if not tasks_dir.exists():
        return None
    for task_file in sorted(tasks_dir.glob("*/task.md")):
        if name_filter and name_filter not in task_file.parent.name:
            continue
        if _is_done(task_file):
            continue
        head = _extract_head_position(task_file)
        if not head.startswith("("):
            return task_file
    return None


def task_done(project_path: Path, name_filter: str = "") -> dict:
    """Check off the current gate and return checked + next gate info.

    Returns dict with keys: task_name, checked, next, task_file.
    On error, returns dict with 'error' key.
    """
    task_file = None

    agent_dir = resolve_agent_dir(project_path)
    session_id = resolve_session_id()
    state_files = [agent_dir / "sessions" / session_id / "current_state"]

    for state_file in state_files:
        if not state_file.exists():
            continue
        task_num = state_file.read_text(encoding="utf-8").strip()
        if not task_num:
            continue
        matches = sorted((agent_dir / "tasks").glob(f"{task_num}-*/task.md"))
        if not matches:
            continue
        candidate = matches[0]
        if name_filter and name_filter not in candidate.parent.name:
            continue
        if _is_done(candidate):
            continue
        head = _extract_head_position(candidate)
        if not head.startswith("("):
            task_file = candidate
            break

    if task_file is None:
        task_file = _find_active_task(project_path, name_filter)
    if not task_file:
        return {"error": "No active task with open gates"}

    task_name = task_file.parent.name
    lines = task_file.read_text(encoding="utf-8").splitlines()

    # Find and check off the first unchecked gate
    checked_text = None
    checked_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- [ ]"):
            checked_text = stripped[6:].strip()
            # Preserve original indentation, just flip the checkbox
            lines[i] = line.replace("- [ ]", "- [x]", 1)
            checked_idx = i
            break

    if checked_text is None:
        return {"error": f"No unchecked gate in {task_name}"}

    # Write back
    task_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Collect next gates (up to 3) after the one we just checked
    upcoming = []
    for line in lines[checked_idx + 1:]:
        stripped = line.strip()
        if stripped.startswith("- [ ]"):
            upcoming.append(stripped[6:].strip())
        elif stripped.endswith(":") and stripped.startswith("- **"):
            upcoming.append(stripped)
        else:
            continue
        if len(upcoming) >= 3:
            break

    return {
        "task_name": task_name,
        "checked": checked_text,
        "upcoming": upcoming,
        "task_file": task_file,
    }


def _extract_progress(task_file: Path) -> str:
    """Count checked/total checkboxes in a task file."""
    try:
        content = task_file.read_text(encoding="utf-8")
        checked = content.count("- [x]") + content.count("- [X]")
        total = checked + content.count("- [ ]")
        return f"{checked}/{total}" if total > 0 else "-"
    except Exception:
        return "-"


def list_tasks(project_path: Path, pending_only: bool = False) -> None:
    """List all tasks with their status and intent."""
    tasks_dir = resolve_agent_dir(project_path) / "tasks"

    if not tasks_dir.exists():
        print("No .agent/tasks/ directory found")
        return

    task_files = sorted(tasks_dir.glob("*/task.md"))

    if not task_files:
        print("No tasks found")
        return

    status_w = 7
    progress_w = 8
    intent_w = 500

    # Collect rows first to compute dynamic name column width
    rows = []
    counts = {"done": 0, "pending": 0, "other": 0}

    for task_file in task_files:
        name = task_file.parent.name
        status = _extract_status(task_file)
        status_key = status.split()[0] if status else "unknown"

        if status_key in ("done", "pending"):
            counts[status_key] += 1
        else:
            counts["other"] += 1

        if pending_only and status_key == "done":
            continue

        intent = _extract_problem(task_file)
        progress = _extract_progress(task_file)

        if len(intent) > intent_w:
            intent = intent[:intent_w-1] + "…"
        if len(status) > status_w:
            status = status[:status_w]

        rows.append((name, status, progress, intent))

    name_w = max((len(r[0]) for r in rows), default=4)
    name_w = max(name_w, 4)  # at least wide enough for "Name"

    print(f"{'Name':<{name_w}} | {'Status':<{status_w}} | {'Progress':<{progress_w}} | Intent")
    print(f"{'-'*name_w}-+-{'-'*status_w}-+-{'-'*progress_w}-+-{'-'*intent_w}")

    for name, status, progress, intent in rows:
        print(f"{name:<{name_w}} | {status:<{status_w}} | {progress:<{progress_w}} | {intent}")

    print("")
    parts = []
    if counts["done"]:
        parts.append(f"{counts['done']} done")
    if counts["pending"]:
        parts.append(f"{counts['pending']} pending")
    if counts["other"]:
        parts.append(f"{counts['other']} other")
    summary = f"Summary: {', '.join(parts)}"
    if pending_only:
        summary += f" (showing {len(rows)} open)"
    print(summary)
    print("Task files: .agent/tasks/<name>/task.md — activate with: pb-tasks work <number>")


def task_status(project_path: Path) -> None:
    """Show head position (first unchecked gate) for each active task."""
    tasks_dir = resolve_agent_dir(project_path) / "tasks"

    if not tasks_dir.exists():
        print("No .agent/tasks/ directory found")
        return

    task_files = sorted(tasks_dir.glob("*/task.md"))

    if not task_files:
        print("No tasks found")
        return

    for task_file in task_files:
        name = task_file.parent.name
        status = _extract_status(task_file)

        if status == "done":
            continue

        head = _extract_head_position(task_file)
        progress = _extract_progress(task_file)

        print(f"{name:<40} | {progress:<8} | {head}")
