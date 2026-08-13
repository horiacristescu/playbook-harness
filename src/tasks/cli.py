"""CLI entry point for standalone tasks management."""
from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from tasks.core import create_task, list_tasks, task_status, PLAYBOOKS, _find_playbook_skill, resolve_session_id, resolve_session_key, resolve_agent_dir
from provider.session_state import (
    SessionStateError,
    clear_navigation_cache,
    ensure_session_record,
    inspect_session_directories,
    iter_session_directories,
    session_state_lock,
    write_navigation_cache,
)
from provider.session_identity import (
    NativeSessionIdentityError,
    resolve_command_session_id,
)
from tasks.provider_detection import (
    DetectionStatus,
    PROVIDER_SPECS,
    ProviderDetection,
    detect_providers,
)
from tasks.provider_contributions import (
    ProviderIntegration,
    build_provider_integrations,
    integration_status,
    skipped_hook_status,
)
from tasks.reconcile import (
    ApplyFailure,
    Contribution,
    ReconcileError,
    apply_reconciliation,
    plan_reconciliation,
    resolve_init_target,
    shared_scaffold_contribution,
)
from tasks.runtime import (
    RUNTIME_COMPAT_SCHEMA,
    runtime_commit,
    runtime_generation_status,
    runtime_identity,
)
from tasks.installed_audit import audit_serving_runtime
from tasks.task_document import TaskDocument, TaskDocumentError, update_task_document
from tasks.task_document import (
    TaskClaimCASMismatch,
    TaskClaimConflict,
    claim_task_document,
    complete_task_document,
    replace_claimed_task_text,
    replace_unclaimed_task_text,
    validate_task_claim,
)


def _append_task_chronology(
    agent_dir: Path,
    key,
    marker: str,
    message: str,
    *event_parts: object,
) -> bool:
    """Best-effort projection after task authority commits; never owns state."""
    from tasks.chat_state import (
        append_chat_event,
        chat_timestamp,
        derive_event_key,
    )

    try:
        append_chat_event(
            agent_dir,
            marker,
            key.provider,
            key.session_id,
            chat_timestamp(),
            message,
            event_key=derive_event_key(marker, key.provider, key.session_id, *event_parts),
        )
    except (OSError, ValueError) as exc:
        print(
            f"Warning: authoritative task state committed but chat chronology "
            f"could not be appended: {exc}",
            file=sys.stderr,
        )
        return False
    return True


def _compact_chat_lines(
    chat_log: Path,
    *,
    last_n: int | None = None,
    width: int = 500,
) -> list[str]:
    """Render human messages as bounded one-line context, excluding task events."""
    from tasks.chat_state import parse_chat_entries

    lines = []
    for entry in parse_chat_entries(chat_log.read_text(encoding="utf-8")):
        if not entry.marker.startswith("M"):
            continue
        body = re.sub(r'<!--\s*/?T\d+\s*-->', '', entry.body)
        body = " ".join(body.split())
        if len(body) > width:
            body = body[:width - 1] + "…"
        identity = (
            f"{entry.provider}/{entry.session_id}"
            if entry.provider and entry.session_id else ""
        )
        tag = f" {entry.session_name}" if entry.session_name else ""
        native = f" {identity}" if identity else ""
        ts = entry.timestamp.removesuffix(" UTC")[:16]
        lines.append(f"[{entry.marker}] {ts} {entry.speaker:<6}{tag}{native} {body}")
    return lines[-last_n:] if last_n is not None else lines


def _build_judge_context(
    project_path: Path,
    *,
    task_file: Path | None,
    task_path: str | None,
    include_mind_map: bool = True,
    max_chars: int = 100_000,
) -> str:
    """Build isolated review context: mind map plus an explicitly named task.

    Interactive bootstrap guidance, recent task lineage, provider onboarding,
    and general chat history are deliberately outside the judge boundary.
    """
    context_parts = []
    if include_mind_map:
        mm_content = _load_mind_map(project_path)
        if mm_content:
            context_parts.append(f"=== MIND_MAP.md ===\n{mm_content}")
    if task_file is not None:
        task_content = task_file.read_text(encoding="utf-8")
        if len(task_content) > max_chars // 2:
            task_content = task_content[:max_chars // 2] + "\n\n[... truncated ...]"
        context_parts.append(f"=== {task_path} ===\n{task_content}")
    context = "\n\n".join(context_parts)
    if len(context) > max_chars:
        context = context[:max_chars] + "\n\n[... truncated ...]"
    return context


_PROVIDER_ALIASES = {
    "claude": "claude",
    "codex": "codex",
    "antigravity": "antigravity",
    "agy": "antigravity",
    "gemini": "antigravity",
    "pi": "pi",
    "omp": "omp",
}


def _session_key_argument(value: str):
    """Parse the public ``provider:native-id`` owner spelling."""
    from provider.session_state import SessionKey
    if ":" not in value:
        raise ValueError("owner must be provider:native-id")
    provider, session_id = value.split(":", 1)
    return SessionKey.from_values(provider, session_id)

_HOOK_SCRIPTS = frozenset(
    {
        "session-start-hook",
        "task-gate-hook",
        "chat-log-hook",
        "state-echo-hook",
        "stop-hook",
        "session-end-hook",
        "codex-user-prompt-hook",
        "codex-stop-hook",
        "codex-apply-patch-hook",
    }
)


def _run_hook_dispatch(args: list[str]) -> int:
    """Replace this process with one reviewed central hook script."""
    if len(args) != 1 or args[0] not in _HOOK_SCRIPTS:
        choices = ", ".join(sorted(_HOOK_SCRIPTS))
        print(f"Error: hook requires one known script ({choices})", file=sys.stderr)
        return 2
    script = Path(__file__).resolve().parents[2] / "scripts" / args[0]
    if not script.is_file():
        print(f"Error: installed hook script is missing: {script}", file=sys.stderr)
        return 1
    # Hook policies use this trusted value to protect the installed checkout.
    # Overwrite (rather than preserve) caller input so a launched agent cannot
    # redirect the permanent runtime guard to some other directory.
    os.environ["PLAYBOOK_RUNTIME_ROOT"] = str(Path(__file__).resolve().parents[2])
    # Bash hooks ship executable. Two Python hook entrypoints intentionally
    # retain ordinary 0644 file mode, so dispatch them through this runtime's
    # interpreter instead of depending on checkout permission bits.
    if os.access(script, os.X_OK):
        os.execv(str(script), [str(script)])
    else:
        os.execv(sys.executable, [sys.executable, str(script)])
    raise AssertionError("os.execv returned")


def _provider_contributions(
    root: Path, detections: tuple[ProviderDetection, ...]
) -> tuple[ProviderIntegration, ...]:
    return build_provider_integrations(root, detections)


def _run_init(args: list[str]) -> int:
    """Reconcile one exact project root without mutating machine state."""
    provider: str | None = None
    explicit: Path | None = None
    include_hooks = True
    legacy_hooks = False
    index = 0
    while index < len(args):
        argument = args[index]
        if argument == "--provider":
            if provider is not None:
                print("Error: --provider may only be specified once", file=sys.stderr)
                return 2
            if index + 1 >= len(args):
                print("Error: --provider requires a name", file=sys.stderr)
                return 2
            requested = args[index + 1].lower()
            provider = _PROVIDER_ALIASES.get(requested)
            if provider is None:
                choices = ", ".join(spec.name for spec in PROVIDER_SPECS)
                print(
                    f"Error: unknown provider '{requested}'. Choose: {choices}",
                    file=sys.stderr,
                )
                return 2
            index += 2
            continue
        if argument == "--no-hooks":
            if not include_hooks:
                print("Error: --no-hooks may only be specified once", file=sys.stderr)
                return 2
            include_hooks = False
            index += 1
            continue
        if argument == "--hooks":
            legacy_hooks = True
            index += 1
            continue
        if argument.startswith("-"):
            print(f"Error: unknown init option: {argument}", file=sys.stderr)
            return 2
        if explicit is not None:
            print("Error: init accepts at most one project path", file=sys.stderr)
            return 2
        explicit = Path(argument)
        index += 1

    if legacy_hooks and not include_hooks:
        print("Error: --hooks and --no-hooks cannot be combined", file=sys.stderr)
        return 2

    try:
        target = resolve_init_target(explicit, Path.cwd())
        shared_contribution = shared_scaffold_contribution(target)
    except ReconcileError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    from tasks.legacy_migration import inspect_legacy_project

    legacy = inspect_legacy_project(
        target, include_hooks=include_hooks, home=Path.home()
    )
    if legacy.conflicts:
        print("Incomplete migration; no project files were changed.", file=sys.stderr)
        for conflict in legacy.conflicts:
            print(f"Manual conflict: {conflict}", file=sys.stderr)
        return 1

    selected_specs = tuple(
        spec for spec in PROVIDER_SPECS if provider is None or spec.name == provider
    )
    detections = detect_providers(specs=selected_specs)

    try:
        integrations = _provider_contributions(target, detections)
        migrated_relatives = {
            intent.relative for intent in legacy.contribution.intents
        }
        if migrated_relatives:
            integrations = tuple(
                ProviderIntegration(
                    item.provider,
                    Contribution(
                        item.contribution.provider,
                        tuple(
                            intent
                            for intent in item.contribution.intents
                            if intent.relative not in migrated_relatives
                        ),
                    ),
                    item.capability,
                    item.detail,
                    item.warnings,
                )
                for item in integrations
            )
        provider_contributions = tuple(item.contribution for item in integrations)
        supported = {
            detection.name
            for detection in detections
            if detection.status == DetectionStatus.SUPPORTED
        }
        contributed: set[str] = set()
        for provider_contribution in provider_contributions:
            if provider_contribution.provider not in supported:
                raise ReconcileError(
                    f"provider contribution is not selected and supported: "
                    f"{provider_contribution.provider}"
                )
            if provider_contribution.provider in contributed:
                raise ReconcileError(
                    f"duplicate provider contribution: {provider_contribution.provider}"
                )
            contributed.add(provider_contribution.provider)
        plan = plan_reconciliation(
            target,
            (shared_contribution, legacy.contribution, *provider_contributions),
            include_hooks=include_hooks,
        )
    except ReconcileError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if plan.conflicts:
        for conflict in plan.conflicts:
            print(
                f"Conflict: {conflict.relative}: {conflict.reason}",
                file=sys.stderr,
            )
        return 1

    print(f"Initializing project: {target.name}")
    try:
        apply_reconciliation(plan)
    except ApplyFailure as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    for operation in plan.operations:
        print(f"  {operation.relative:<34} {operation.state.value}")
    for relative in legacy.migrated:
        action = "retired" if "monitor" in relative else "migrated"
        print(f"  {relative:<34} {action}")
    integration_by_name = {item.provider: item for item in integrations}
    for detection in detections:
        if detection.status == DetectionStatus.SUPPORTED:
            version = ".".join(str(part) for part in detection.version or ())
            detail = f" {version}" if version else ""
            integration = integration_by_name.get(detection.name)
            if detection.name not in contributed or integration is None:
                status = "integration pending"
            else:
                states = tuple(
                    operation.state
                    for operation in plan.operations
                    if detection.name in operation.owners
                )
                status = integration_status(
                    integration,
                    states,
                    include_hooks=include_hooks,
                    skipped_hook_status=skipped_hook_status(target, integration),
                )
            capability_detail = (
                f" ({integration.detail})"
                if integration is not None
                and status == integration.capability.value
                and integration.detail
                else ""
            )
            print(
                f"  provider {detection.name:<20} "
                f"{status}{detail}{capability_detail}"
            )
            for warning in integration.warnings if integration is not None else ():
                print(f"    warning: {warning}")
        elif detection.status == DetectionStatus.ABSENT:
            print(f"  provider {detection.name:<20} skipped (not installed)")
        else:
            print(f"  provider {detection.name:<20} unsupported ({detection.detail})")
    if not include_hooks:
        print("  hooks                              preserved (--no-hooks)")
    return 0


def _state_file(project_path: Path) -> Path:
    """Return the current provider-qualified session navigation cache."""
    agent_dir = resolve_agent_dir(project_path)
    key = resolve_session_key()
    record, _ = ensure_session_record(agent_dir, key.provider, key.session_id)
    return record.parent / "current_state"


def _session_surface(agent_dir: Path, key) -> str:
    """Describe session→task state without making the cache authoritative."""
    from tasks.task_document import validate_task_claim, TaskDocumentError
    record, _ = ensure_session_record(agent_dir, key.provider, key.session_id)
    state = record.parent / "current_state"
    handle = f"{key.provider}:{key.session_id}"
    if not state.exists():
        return f"Session: {handle} | task: unclaimed"
    try:
        number = state.read_text(encoding="utf-8").strip()
        task_file = validate_task_claim(agent_dir, key, number)
    except (OSError, TaskDocumentError) as exc:
        return f"Session: {handle} | task: AUTHORITY MISMATCH — {exc}"
    return f"Session: {handle} | task: {number} ({task_file.parent.name})"


def _session_store_surface(agent_dir: Path) -> str:
    """Report recognized records and inert legacy/malformed directories."""
    sessions = agent_dir / "sessions"
    if not sessions.exists():
        return "Session store: 0 recognized | 0 inert legacy/malformed"
    recognized = list(iter_session_directories(agent_dir))
    recognized_paths = {path for _, path in recognized}
    inert = sum(
        1 for path in sessions.iterdir()
        if path.is_dir() and not path.is_symlink() and path not in recognized_paths
    )
    return (
        f"Session store: {len(recognized)} recognized provider-native record(s)"
        f" | {inert} inert legacy/malformed director{'y' if inert == 1 else 'ies'}"
    )


def _playbook_skill_catalog() -> tuple[tuple[str, str], ...]:
    """Return the installed canonical skill names and trigger descriptions."""
    skill_root = Path(__file__).resolve().parents[2] / "skills"
    catalog = []
    if not skill_root.is_dir():
        return ()
    for skill_file in sorted(skill_root.glob("*/SKILL.md")):
        text = skill_file.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("---\n"):
            continue
        closing = text.find("\n---\n", 4)
        if closing < 0:
            continue
        frontmatter = text[4:closing].splitlines()
        name = ""
        description_parts: list[str] = []
        collecting = False
        for line in frontmatter:
            if line.startswith("name:"):
                name = line.partition(":")[2].strip()
                collecting = False
            elif line.startswith("description:"):
                value = line.partition(":")[2].strip()
                collecting = value in {">", "|"}
                if value and not collecting:
                    description_parts.append(value.strip('"\''))
            elif collecting and line.startswith("  "):
                description_parts.append(line.strip())
            elif collecting:
                collecting = False
        if name:
            catalog.append((name, " ".join(description_parts)))
    return tuple(catalog)


def _capture_recent_chat(project_path: Path, max_messages: int = 10,
                         max_gap_seconds: int = 10800) -> list[str]:
    """Capture recent chat_log messages for task attribution.

    Scans backwards from end of chat_log.md. Stops at:
    - Previous 'tasks done' or 'tasks work done' in message text
    - A time gap > max_gap_seconds (default 3h) between consecutive messages
    - max_messages reached (default 10)

    Returns list of message blocks (most recent last), each as:
    "**[MNNN]** [timestamp]\\n<text truncated to 200 chars>"
    """
    from datetime import datetime
    from tasks.chat_state import parse_chat_entries

    chat_log = resolve_agent_dir(project_path) / "chat_log.md"
    if not chat_log.exists():
        return []

    messages = []
    for entry in parse_chat_entries(chat_log.read_text(encoding="utf-8")):
        if not entry.marker.startswith("M"):
            continue
        timestamp_str = entry.timestamp.removesuffix(" UTC")
        try:
            ts = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        messages.append((entry.marker, ts, timestamp_str, entry.body))

    if not messages:
        return []

    # Scan backwards
    captured = []
    prev_ts = None
    for msg_id, ts, ts_str, text in reversed(messages):
        # Stop at time gap
        if prev_ts is not None:
            gap = (prev_ts - ts).total_seconds()
            if gap > max_gap_seconds:
                break
        prev_ts = ts

        # Stop at task-done marker
        text_lower = text.lower()
        if "tasks done" in text_lower or "tasks work done" in text_lower:
            break

        # Truncate long messages
        display_text = text[:200] + "..." if len(text) > 200 else text
        captured.append(f"**[{msg_id}]** [{ts_str}]\n{display_text}")

        if len(captured) >= max_messages:
            break

    # Reverse to chronological order
    captured.reverse()
    return captured


def _render_chat_into_task(content: str, messages: list[str]) -> str:
    """Return task text with the managed recent-chat block refreshed."""
    if not messages:
        return content

    import re

    def _utf8_safe(text: str) -> str:
        """Replace non-UTF-8-survivable code points like lone surrogates."""
        return text.encode("utf-8", errors="replace").decode("utf-8")

    start_marker = "<!-- playbook-recent-chat:start -->"
    end_marker = "<!-- playbook-recent-chat:end -->"
    chat_block = (
        f"\n{start_marker}\n"
        "### Recent Chat (auto-captured at activation — review and remove unrelated)\n"
    )
    for msg in messages:
        chat_block += f"\n{_utf8_safe(msg)}\n"
    chat_block += f"\n{end_marker}\n"

    # Remove the managed block before locating the structural separator. Chat
    # text may itself be exactly "---", so the first separator is not stable.
    content = re.sub(
        rf"\n{re.escape(start_marker)}\n.*?\n{re.escape(end_marker)}\n",
        "\n",
        content,
        flags=re.DOTALL,
    )
    design = content.find("\n## Design Phase")
    separator = content.rfind("\n---\n", 0, design if design >= 0 else len(content))
    if separator >= 0:
        references = content[:separator]
        # Adopt blocks written before explicit managed markers were introduced.
        references = re.sub(
            r'\n### Recent Chat \(auto-captured at activation — review and remove unrelated\)\n.*\Z',
            "",
            references,
            flags=re.DOTALL,
        )
        content = references.rstrip() + "\n" + chat_block + content[separator:]
    return _utf8_safe(content)


def _inject_chat_into_task(task_file: Path, messages: list[str]) -> None:
    """Compatibility helper for unclaimed/task-creation paths and unit tests."""
    original = task_file.read_text(encoding="utf-8")
    updated = _render_chat_into_task(original, messages)
    if updated != original:
        task_file.write_text(updated, encoding="utf-8")


def _load_mind_map(project_path: Path, max_chars: int = 25000) -> str | None:
    """Load MIND_MAP.md content. If over max_chars, keep head + tail, drop middle.

    Head has overview nodes [1]-[4]; tail has recent additions and roadmap.
    The middle is the most expendable, so we trim there on a line boundary.

    Set PLAYBOOK_MINDMAP_MAX env var to override max_chars (0 = suppress entirely).
    """
    env_max = os.environ.get("PLAYBOOK_MINDMAP_MAX")
    if env_max is not None:
        max_chars = int(env_max)
        if max_chars == 0:
            return None
    mind_map = project_path / "MIND_MAP.md"
    if not mind_map.exists():
        return None
    content = mind_map.read_text(encoding="utf-8")
    if len(content) <= max_chars:
        return content

    max_omitted_digits = len(str(content.count("\n")))
    marker_budget = len(f"\n\n[... {'9' * max_omitted_digits} lines omitted ...]\n")
    available = max(max_chars - marker_budget, 0)
    if available == 0:
        return content[:max_chars]

    # Keep 60% head, 40% tail — overview nodes are denser at the top.
    head_budget = int(available * 0.6)
    tail_budget = available - head_budget

    # Snap inward to line boundaries so the head/tail stay within budget.
    head_end = content.rfind("\n", 0, head_budget)
    if head_end < 0:
        head_end = head_budget
    tail_start = content.find("\n", len(content) - tail_budget)
    if tail_start < 0:
        tail_start = len(content) - tail_budget
    else:
        tail_start += 1
    head = content[:head_end]
    tail = content[tail_start:]
    omitted = content[head_end:tail_start].count("\n")
    marker = f"\n\n[... {omitted} lines omitted ...]\n"
    result = f"{head}{marker}{tail}"
    if len(result) > max_chars:
        overflow = len(result) - max_chars
        if overflow < len(tail):
            tail = tail[overflow:]
        else:
            head = head[:max(len(head) - (overflow - len(tail)), 0)]
            tail = ""
        result = f"{head}{marker}{tail}"
    return result[:max_chars]


def find_project_root() -> Path:
    """Find project root by looking for the nearest .agent/tasks/ directory."""
    cwd = Path.cwd()

    for p in [cwd, *cwd.parents]:
        agent = p / ".agent"
        if (agent / "tasks").exists():
            return p
        # Multi-user layout: .agent/<user>/tasks/
        if agent.is_dir():
            for sub in agent.iterdir():
                if sub.is_dir() and (sub / "tasks").exists():
                    return p

    # Fall back to cwd (create_task will make .agent/tasks/)
    return cwd


def _cleanup_legacy_flat_session_files(project_path: Path) -> None:
    """Remove only pre-session-layout flat state from the agent root.

    Provider-native sessions are resumable conversations. Process liveness and
    file age cannot prove that their state is dead, so ordinary CLI entry never
    garbage-collects a directory under ``sessions/``.
    """
    agent_dir = resolve_agent_dir(project_path)
    for pattern in (".hook_counters.*", "current_state", "current_state.*"):
        for f in agent_dir.glob(pattern):
            if f.is_file():
                try:
                    f.unlink()
                except OSError:
                    pass

def _panel_triage_frame() -> list[str]:
    """Return the lines to append to a panel-review judge.md so the reading
    agent meets the triage discipline alongside the findings.

    Same wording for plan and impl modes (the panel-review assembly is shared);
    mirrors the per-task pushback gate from `template.judge_section()` /
    `template.judge_impl_section()` but lives in the file the agent actually
    reads after the panel runs.
    """
    bar = "═" * 60
    return [
        bar,
        "## Triage",  # No indent — must match `^## ` line-start parsers (impl-review F4).
        bar + "\n",
        (
            "These findings are opinion, not gospel. Before applying any of "
            "them, decide per-finding: real correctness issue, speculative "
            "concern, or wrong call. Document accept (with rationale) / park "
            "(with rationale) / reject (with rationale). Verify file:line "
            "claims before applying — panel judges sometimes cite wrong "
            "locations. The panel doesn't live with the outcomes — you do. "
            "Push back where you have concrete evidence the panel doesn't."
        ),
        "",
    ]


def _cmd_prepare_merge(project_path: Path, target: str, dry_run: bool) -> None:
    """Prepare current branch's Playbook state to merge cleanly into target."""
    import subprocess
    import re as _re

    agent_dir = resolve_agent_dir(project_path)

    # --- Shared: merge base ---
    try:
        merge_base = subprocess.check_output(
            ["git", "-C", str(project_path), "merge-base", "HEAD", target],
            text=True,
        ).strip()
    except subprocess.CalledProcessError:
        print(f"Error: could not compute merge base with '{target}'. Is '{target}' a valid branch?",
              file=sys.stderr)
        sys.exit(1)

    # --- Step 1: Task renumbering (placeholder) ---
    _prepare_merge_tasks(project_path, agent_dir, target, merge_base, dry_run)

    # --- Step 2: Chat log re-sequencing (placeholder) ---
    try:
        _prepare_merge_chatlog(project_path, agent_dir, target, merge_base, dry_run)
    except (OSError, RuntimeError, SessionStateError, TaskDocumentError) as exc:
        print(f"Error: prepare-merge chat transaction refused: {exc}", file=sys.stderr)
        sys.exit(1)

    # --- Step 3: MIND_MAP collision report (placeholder) ---
    _prepare_merge_mindmap(project_path, target, merge_base)

    if dry_run:
        print("(dry-run — no files written)")


def _git_ls_tasks(project_path: Path, ref: str, agent_dir: Path) -> dict[int, str]:
    """Return {task_number: dir_name} for tasks present at git ref. Empty dict if path absent."""
    import subprocess
    import re
    agent_dir_rel = str(agent_dir.relative_to(project_path))
    tasks_path = agent_dir_rel + "/tasks/"
    try:
        out = subprocess.check_output(
            ["git", "-C", str(project_path), "ls-tree", "--name-only", ref, tasks_path],
            text=True, stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return {}
    result: dict[int, str] = {}
    for entry in out.splitlines():
        name = entry.rstrip("/").split("/")[-1]
        m = re.match(r"^(\d+)-", name)
        if m:
            result[int(m.group(1))] = name
    return result


def _prepare_merge_tasks(project_path: Path, agent_dir: Path, target: str,
                         merge_base: str, dry_run: bool) -> None:
    import re

    base_tasks = _git_ls_tasks(project_path, merge_base, agent_dir)
    target_tasks = _git_ls_tasks(project_path, target, agent_dir)

    # Current tasks: scan working tree directly
    tasks_dir = agent_dir / "tasks"
    current_tasks: dict[int, str] = {}
    if tasks_dir.exists():
        for d in tasks_dir.iterdir():
            if d.is_dir():
                m = re.match(r"^(\d+)-", d.name)
                if m:
                    current_tasks[int(m.group(1))] = d.name

    new_on_current = {n: name for n, name in current_tasks.items() if n not in base_tasks}
    new_on_target = {n: name for n, name in target_tasks.items() if n not in base_tasks}
    collisions = set(new_on_current) & set(new_on_target)

    if not collisions:
        print("Tasks: no collisions — already clean.")
        return

    # Assign new numbers starting after the highest number on target (across all tasks, not just new)
    max_target = max(target_tasks) if target_tasks else 0
    rename_map: dict[int, int] = {}
    next_num = max_target + 1
    for old_num in sorted(collisions):
        rename_map[old_num] = next_num
        next_num += 1

    print("Tasks: " + str(len(collisions)) + " collision(s) to renumber: "
          + ", ".join(f"T{n}→T{rename_map[n]}" for n in sorted(collisions)))

    if dry_run:
        for old_num in sorted(rename_map):
            old_name = current_tasks[old_num]
            new_name = old_name.replace(str(old_num) + "-", str(rename_map[old_num]) + "-", 1)
            print(f"  [dry-run] rename {old_name} → {new_name}")
        return

    try:
        _renumber_tasks_transactionally(agent_dir, current_tasks, rename_map)
    except (OSError, RuntimeError, SessionStateError, TaskDocumentError) as exc:
        print(f"Error: prepare-merge task transaction refused: {exc}", file=sys.stderr)
        sys.exit(1)


def _rewrite_task_references(text: str, rename_map: dict[int, int]) -> str:
    import re
    # Descending order prevents a smaller old number from cascading inside a
    # newly written larger number.
    for old_num in sorted(rename_map, reverse=True):
        new_num = rename_map[old_num]
        text = re.sub(rf"\bT{old_num}\b", f"T{new_num}", text)
        text = re.sub(
            rf"\btask {old_num}\b", f"task {new_num}", text,
            flags=re.IGNORECASE,
        )
        text = re.sub(rf"\b{old_num}(?=-[a-z])", str(new_num), text)
        text = re.sub(rf"\bG{old_num}:(\d+)\b", rf"G{new_num}:\1", text)
    return text


def _prepare_merge_atomic_write(path: Path, content: bytes) -> None:
    """Publish one prepare-merge file without exposing a partial write."""
    import tempfile
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _prepare_merge_snapshot_bytes(path: Path) -> bytes | None:
    """Read a merge-owned auxiliary file, distinguishing absence from empty."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _prepare_merge_require_snapshot(
    path: Path, expected: bytes | None, surface: str
) -> None:
    """Refuse rather than overwrite a writer that did not honor our lock."""
    if _prepare_merge_snapshot_bytes(path) != expected:
        raise TaskDocumentError(
            f"prepare-merge refused: concurrent {surface} change at {path}"
        )


def _renumber_tasks_transactionally(
    agent_dir: Path,
    current_tasks: dict[int, str],
    rename_map: dict[int, int],
) -> None:
    """Renumber unclaimed tasks and all reverse pointers as one rollback unit."""
    from contextlib import ExitStack
    import re
    from tasks.task_document import task_authority_lock

    tasks_dir = agent_dir / "tasks"
    task_files = sorted(
        path for path in tasks_dir.glob("*/task.md") if path.is_file()
    )
    renamed: list[tuple[Path, Path]] = []
    with ExitStack() as locks:
        # Lock every task whose references may change, not merely the colliding
        # directory. This shares the exact lock used by claim/handoff edits.
        for task_file in task_files:
            locks.enter_context(task_authority_lock(task_file))

        original_task_bytes = {path: path.read_bytes() for path in task_files}
        original_task_text = {
            path: content.decode("utf-8")
            for path, content in original_task_bytes.items()
        }
        recognized_sessions = list(iter_session_directories(agent_dir))
        # Hold every auxiliary-state authority through snapshot, publication,
        # and rollback. Cache/offset writers share the per-session record lock;
        # all shipped chat appenders share the project chat lock.
        for _, session_dir in recognized_sessions:
            locks.enter_context(session_state_lock(session_dir))
        from tasks.chat_state import chat_log_lock
        locks.enter_context(chat_log_lock(agent_dir))
        cache_bytes: dict[Path, bytes] = {}
        offset_bytes: dict[Path, bytes] = {}
        for _, session_dir in recognized_sessions:
            state = session_dir / "current_state"
            if state.exists():
                cache_bytes[state] = state.read_bytes()
            offset = session_dir / "chat_log_offset"
            if offset.exists():
                offset_bytes[offset] = offset.read_bytes()
        chat_log = agent_dir / "chat_log.md"
        chat_bytes = chat_log.read_bytes() if chat_log.exists() else None

        for old_num in sorted(rename_map):
            task_file = tasks_dir / current_tasks[old_num] / "task.md"
            document = TaskDocument.parse(original_task_text[task_file])
            owner = document.live_owner
            if owner is not None:
                raise TaskDocumentError(
                    f"claimed task {task_file} is owned by "
                    f"{owner.provider}:{owner.session_id}"
                )

        updated_tasks: dict[Path, bytes] = {}
        destinations: dict[Path, Path] = {}
        directory_moves: list[tuple[Path, Path]] = []
        for old_num in sorted(rename_map):
            old_dir = tasks_dir / current_tasks[old_num]
            new_num = rename_map[old_num]
            new_name = str(new_num) + old_dir.name[len(str(old_num)):]
            new_dir = tasks_dir / new_name
            if new_dir.exists():
                raise TaskDocumentError(f"renumber destination already exists: {new_dir}")
            directory_moves.append((old_dir, new_dir))

        for path, text in original_task_text.items():
            destination = path
            for old_dir, new_dir in directory_moves:
                if path.parent == old_dir:
                    destination = new_dir / path.name
                    old_num = int(old_dir.name.partition("-")[0])
                    text = re.sub(
                        rf"^# {old_num}(?=[\s\-]|$)",
                        f"# {rename_map[old_num]}", text, count=1,
                        flags=re.MULTILINE,
                    )
                    break
            destinations[path] = destination
            updated_tasks[destination] = _rewrite_task_references(
                text, rename_map
            ).encode("utf-8")

        updated_caches: dict[Path, bytes] = {}
        for state, content in cache_bytes.items():
            try:
                task_num = int(content.decode("utf-8").strip())
            except (UnicodeDecodeError, ValueError):
                continue
            if task_num in rename_map:
                updated_caches[state] = f"{rename_map[task_num]}\n".encode()

        updated_chat = None
        if chat_bytes is not None:
            updated_chat = _rewrite_task_references(
                chat_bytes.decode("utf-8"), rename_map
            ).encode("utf-8")

        published_aux: dict[Path, tuple[bytes | None, bytes]] = {}
        deleted_offsets: dict[Path, bytes] = {}
        try:
            for old_dir, new_dir in directory_moves:
                old_dir.rename(new_dir)
                renamed.append((old_dir, new_dir))
            for destination, content in updated_tasks.items():
                current = content
                if destination.read_bytes() != current:
                    _prepare_merge_atomic_write(destination, current)
            if updated_chat is not None and updated_chat != chat_bytes:
                _prepare_merge_require_snapshot(chat_log, chat_bytes, "chat-log")
                published_aux[chat_log] = (chat_bytes, updated_chat)
                _prepare_merge_atomic_write(chat_log, updated_chat)
            for state, content in updated_caches.items():
                if _prepare_merge_snapshot_bytes(state) != content:
                    _prepare_merge_require_snapshot(
                        state, cache_bytes[state], "session-cache"
                    )
                    published_aux[state] = (cache_bytes[state], content)
                    _prepare_merge_atomic_write(state, content)
            for offset, content in offset_bytes.items():
                _prepare_merge_require_snapshot(offset, content, "chat-log-offset")
                offset.unlink()
                deleted_offsets[offset] = content
        except BaseException as original_error:
            # Restore content while renamed directories still exist, then put
            # directory names back. Auxiliary rollback is itself a CAS: an
            # uncooperative writer is preserved and reported, never erased.
            for original, content in original_task_bytes.items():
                current_path = destinations.get(original, original)
                if current_path.exists():
                    _prepare_merge_atomic_write(current_path, content)
            rollback_conflicts: list[Path] = []
            for path, (before, published) in published_aux.items():
                current = _prepare_merge_snapshot_bytes(path)
                if current == published:
                    if before is None:
                        path.unlink(missing_ok=True)
                    else:
                        _prepare_merge_atomic_write(path, before)
                elif current != before:
                    rollback_conflicts.append(path)
            for path, content in deleted_offsets.items():
                current = _prepare_merge_snapshot_bytes(path)
                if current is None:
                    _prepare_merge_atomic_write(path, content)
                elif current != content:
                    rollback_conflicts.append(path)
            for old_dir, new_dir in reversed(renamed):
                if new_dir.exists():
                    new_dir.rename(old_dir)
            if rollback_conflicts:
                paths = ", ".join(str(path) for path in rollback_conflicts)
                raise TaskDocumentError(
                    "prepare-merge rollback preserved concurrent changes at " + paths
                ) from original_error
            raise


def _prepare_merge_chatlog(project_path: Path, agent_dir: Path, target: str,
                           merge_base: str, dry_run: bool) -> None:
    import subprocess
    import re

    agent_dir_rel = str(agent_dir.relative_to(project_path))

    def _git_show_text(ref: str, rel_path: str) -> str:
        try:
            return subprocess.check_output(
                ["git", "-C", str(project_path), "show", f"{ref}:{rel_path}"],
                text=True, stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            return ""

    def _last_mid(text: str) -> int:
        mids = re.findall(r"\*\*\[M(\d+)\]\*\*", text)
        return max(int(m) for m in mids) if mids else 0

    chat_log_rel = agent_dir_rel + "/chat_log.md"
    base_last = _last_mid(_git_show_text(merge_base, chat_log_rel))
    target_last = _last_mid(_git_show_text(target, chat_log_rel))

    from tasks.chat_state import chat_log_lock
    with chat_log_lock(agent_dir):
        chat_log = agent_dir / "chat_log.md"
        if not chat_log.exists():
            print("Chat log: not found — skipping.")
            return

        chat_before = chat_log.read_bytes()
        current_text = chat_before.decode("utf-8")
        new_mids = [int(m) for m in re.findall(r"\*\*\[M(\d+)\]\*\*", current_text) if int(m) > base_last]

        if not new_mids:
            print("Chat log: no new entries beyond merge base — already clean.")
            return

        # Idempotency: if new entries already start beyond target's last MID, we're done
        if min(new_mids) > target_last:
            print("Chat log: new entries already positioned beyond target's last MID — already clean.")
            return

        offset = target_last - base_last
        if offset <= 0:
            print("Chat log: target has not advanced beyond merge base — no re-sequencing needed.")
            return

        def _reseq(m: "re.Match[str]") -> str:
            mid = int(m.group(1))
            if mid > base_last:
                width = max(len(m.group(1)), len(str(mid + offset)))
                return f"**[M{mid + offset:0{width}d}]**"
            return m.group(0)

        updated = re.sub(r"\*\*\[M(\d+)\]\*\*", _reseq, current_text)
        new_highest = max(new_mids) + offset

        print(f"Chat log: re-sequencing {len(new_mids)} new entr{'y' if len(new_mids)==1 else 'ies'} "
              f"(offset +{offset}, new highest M{new_highest}).")

        if dry_run:
            return

        counter = agent_dir / "chat_log_counter"
        counter_before = _prepare_merge_snapshot_bytes(counter)
        chat_after = updated.encode("utf-8")
        counter_after = f"{new_highest}\n".encode("utf-8")
        published: dict[Path, tuple[bytes | None, bytes]] = {}
        try:
            _prepare_merge_require_snapshot(chat_log, chat_before, "chat-log")
            published[chat_log] = (chat_before, chat_after)
            _prepare_merge_atomic_write(chat_log, chat_after)
            _prepare_merge_require_snapshot(counter, counter_before, "chat-counter")
            published[counter] = (counter_before, counter_after)
            _prepare_merge_atomic_write(counter, counter_after)
        except BaseException as original_error:
            conflicts: list[Path] = []
            for path, (before, intended) in published.items():
                current = _prepare_merge_snapshot_bytes(path)
                if current == intended:
                    if before is None:
                        path.unlink(missing_ok=True)
                    else:
                        _prepare_merge_atomic_write(path, before)
                elif current != before:
                    conflicts.append(path)
            if conflicts:
                paths = ", ".join(str(path) for path in conflicts)
                raise TaskDocumentError(
                    "prepare-merge chat rollback preserved concurrent changes at "
                    + paths
                ) from original_error
            raise


def _prepare_merge_mindmap(project_path: Path, target: str, merge_base: str) -> None:
    import subprocess
    import re
    import hashlib

    def _git_show_text(ref: str, rel_path: str) -> str:
        try:
            return subprocess.check_output(
                ["git", "-C", str(project_path), "show", f"{ref}:{rel_path}"],
                text=True, stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            return ""

    def _parse_nodes(text: str) -> dict[int, str]:
        nodes: dict[int, str] = {}
        current_id: int | None = None
        current_lines: list[str] = []
        for line in text.splitlines(keepends=True):
            m = re.match(r"^\[(\d+)\] ", line)
            if m:
                if current_id is not None:
                    nodes[current_id] = "".join(current_lines)
                current_id = int(m.group(1))
                current_lines = [line]
            elif current_id is not None:
                current_lines.append(line)
        if current_id is not None and current_lines:
            nodes[current_id] = "".join(current_lines)
        return nodes

    def _h(text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()

    collision_found = False
    for filename in ("MIND_MAP.md",):
        base_nodes = _parse_nodes(_git_show_text(merge_base, filename))
        target_nodes = _parse_nodes(_git_show_text(target, filename))
        cur_file = project_path / filename
        current_nodes = _parse_nodes(cur_file.read_text(encoding="utf-8") if cur_file.exists() else "")

        all_ids = set(base_nodes) | set(target_nodes) | set(current_nodes)
        changed_current = {n for n in all_ids if _h(current_nodes.get(n, "")) != _h(base_nodes.get(n, ""))}
        changed_target = {n for n in all_ids if _h(target_nodes.get(n, "")) != _h(base_nodes.get(n, ""))}
        collisions = sorted(changed_current & changed_target)
        if not collisions:
            continue

        collision_found = True
        print(f"\n{filename}: {len(collisions)} node collision(s) requiring manual synthesis:")
        for node_id in collisions:
            tgt = target_nodes.get(node_id, "(absent on target)")
            cur = current_nodes.get(node_id, "(absent on current)")
            print(f"\n  ── [{node_id}] {target} (target) ──")
            for line in tgt.splitlines():
                print(f"  {line}")
            print(f"\n  ── [{node_id}] HEAD (current) ──")
            for line in cur.splitlines():
                print(f"  {line}")

    if not collision_found:
        print("MIND_MAP: no node collisions.")


def print_usage():
    from tasks.template import usage_text
    print(usage_text())


def _gate_bounce(task_id: str, task_file, action: str) -> bool:
    """If `task_file` has open (unchecked) gates, print a steering message and
    return True (the caller should abort). Returns False when all gates are
    checked. The `--force` decision is the caller's — this only reports.
    """
    from tasks.core import _extract_head_position
    head = _extract_head_position(task_file)
    if head == "(all gates checked)":
        return False
    try:
        from tasks.task_document import TaskDocument
        document = TaskDocument.parse(task_file.read_text(encoding="utf-8"))
        open_count = sum(not gate.checked for gate in document.gates)
    except (OSError, TaskDocumentError):
        open_count = 0
    print(
        f"Blocked: task {task_id} has {open_count} open gate(s) — {action} needs them finalized.",
        file=sys.stderr,
    )
    print(f"  Next open gate: {head}", file=sys.stderr)
    print(
        "  Finish them (check the boxes in task.md), then retry — or override with --force.",
        file=sys.stderr,
    )
    return True


def main():
    # Force utf-8 on Windows where the default console encoding (cp1252) chokes on → and emoji.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help", "help"):
        print_usage()
        return

    cmd = args[0]
    cmd_args = args[1:]

    # Init deliberately targets cwd/an explicit directory rather than adopting
    # an initialized parent. Even legacy flat-state cleanup would violate that local-only
    # contract, so ordinary upward-discovery maintenance starts after init.
    if cmd not in ("init", "hook", "runtime-info", "runtime-audit"):
        _cleanup_legacy_flat_session_files(find_project_root())

    if cmd == "hook":
        sys.exit(_run_hook_dispatch(cmd_args))

    if cmd == "runtime-info":
        if cmd_args:
            print("Error: runtime-info accepts no arguments", file=sys.stderr)
            sys.exit(2)
        try:
            identity = runtime_identity()
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(identity, sort_keys=True))
        return

    if cmd == "runtime-audit":
        if cmd_args:
            print("Error: runtime-audit accepts no arguments", file=sys.stderr)
            sys.exit(2)
        runtime_root = Path(__file__).resolve().parents[2]
        errors = audit_serving_runtime(runtime_root)
        if errors:
            print("Installed runtime audit failed:", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            sys.exit(1)
        print("Installed runtime audit passed")
        return

    if cmd == "work":
        if not cmd_args:
            print("Error: 'work' requires a task number or 'done'", file=sys.stderr)
            print("Usage: pb-tasks work <number> | pb-tasks work done", file=sys.stderr)
            sys.exit(1)

        task_num = cmd_args[0]
        if task_num != "done" and task_num.isdigit():
            task_num = task_num.zfill(3)
        force = any(a in ("--force", "-f") for a in cmd_args[1:])
        transfer_flags = [
            flag for flag in ("--handoff-from", "--recover-from")
            if flag in cmd_args[1:]
        ]
        if len(transfer_flags) > 1:
            print("Error: choose either --handoff-from or --recover-from", file=sys.stderr)
            sys.exit(2)
        expected_owner = None
        transfer_kind = None
        if transfer_flags:
            transfer_kind = transfer_flags[0][2:-5]
            position = cmd_args.index(transfer_flags[0])
            if position + 1 >= len(cmd_args):
                print(f"Error: {transfer_flags[0]} requires provider:native-id", file=sys.stderr)
                sys.exit(2)
            try:
                expected_owner = _session_key_argument(cmd_args[position + 1])
            except (KeyError, ValueError) as exc:
                print(f"Error: invalid expected owner: {exc}", file=sys.stderr)
                sys.exit(2)
        project_path = find_project_root()

        # Handle 'tasks work done' - deactivate current task and set Status in task.md
        if task_num == "done":
            agent_dir = resolve_agent_dir(project_path)
            session_state = _state_file(project_path)
            current_key = resolve_session_key()

            # Find the active task from session state file
            prev_task = session_state.read_text(encoding="utf-8").strip() if session_state.exists() else None

            if prev_task:
                # Resolve the cache through the same unique task.md authority
                # used by governed edits before inspecting or mutating gates.
                try:
                    task_file = validate_task_claim(
                        agent_dir, current_key, prev_task
                    )
                except (OSError, TaskDocumentError) as exc:
                    print(
                        f"Error: cannot close cached task {prev_task}: {exc}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                if not force and _gate_bounce(prev_task, task_file, "closing this task"):
                    sys.exit(1)
                # Codex Stop compares the dirty-code tree with a prompt-start
                # baseline.  Commit the authorized state before removing this
                # session's active-task pointer, otherwise a task completed in
                # one turn is falsely reported as unowned work at turn end.
                if current_key.provider == "codex":
                    from provider.codex_hooks import checkpoint_turn_baselines

                    try:
                        checkpoint_turn_baselines(
                            project_path, current_key.session_id
                        )
                    except OSError as exc:
                        print(
                            f"Error: cannot checkpoint Codex Stop state before "
                            f"closing task {prev_task}: {exc}",
                            file=sys.stderr,
                        )
                        sys.exit(1)
                try:
                    completed = complete_task_document(task_file, current_key)
                except (OSError, TaskDocumentError) as exc:
                    print(f"Error: cannot close {task_file}: {exc}", file=sys.stderr)
                    sys.exit(1)
                _append_task_chronology(
                    agent_dir,
                    current_key,
                    f"T{prev_task}:release",
                    f"completed task {prev_task}",
                    len(completed.sessions),
                    "forced" if force else "gates-complete",
                    task_file.stat().st_mtime_ns,
                )
                # The native session record is a durable resume handle. Only
                # clear this session's rebuildable navigation cache; ownership
                # release becomes task-authoritative in the claim gate.
                try:
                    clear_navigation_cache(
                        agent_dir, current_key, expected_task=prev_task
                    )
                except (OSError, SessionStateError) as exc:
                    print(
                        f"Error: task {prev_task} is done but cache cleanup failed; "
                        f"rerun this command: {exc}", file=sys.stderr,
                    )
                    sys.exit(1)
                print(f"Task {prev_task} done.")
            else:
                print("No active task.")
            print("Code edits blocked until: pb-tasks work <N>")
            return

        # Resolve one exact task document before any activation effect. This
        # refuses duplicate numbers and malformed managed sections rather than
        # routing around them through a first glob/head-position guess.
        from tasks.task_document import resolve_task_document
        try:
            task_file = resolve_task_document(
                resolve_agent_dir(project_path), task_num
            )
            candidate_text = task_file.read_text(encoding="utf-8")
            candidate_document = TaskDocument.parse(candidate_text)
        except (OSError, TaskDocumentError) as exc:
            print(f"Error: cannot resolve task {task_num}: {exc}", file=sys.stderr)
            sys.exit(1)
        if candidate_document.status == "done":
            # Reopen only after all activation checks and stub expansion.
            print(f"Note: task {task_num} was marked done — reopening.")
        elif "<!-- stub:" not in candidate_text and candidate_document.head_position == "(all gates checked)":
            print(f"Task {task_num} has no open gates.", file=sys.stderr)
            sys.exit(1)

        # Auto-close previous task if all gates are checked
        agent_dir = resolve_agent_dir(project_path)
        agent_dir.mkdir(parents=True, exist_ok=True)
        try:
            TaskDocument.parse(task_file.read_text(encoding="utf-8"))
        except (OSError, TaskDocumentError) as exc:
            print(f"Error: cannot activate {task_file}: {exc}", file=sys.stderr)
            sys.exit(1)
        session_state = _state_file(project_path)
        prev_task = None
        if session_state.exists():
            prev_task = session_state.read_text(encoding="utf-8").strip()
        if prev_task and prev_task != task_num:
            from tasks.core import _extract_head_position, _extract_status
            prev_matches = list((agent_dir / "tasks").glob(f"{prev_task}-*/task.md"))
            if prev_matches:
                prev_file = prev_matches[0]
                prev_status = _extract_status(prev_file)
                prev_head = _extract_head_position(prev_file)
                if prev_head == "(all gates checked)":
                    if prev_status != "done":
                        # Auto-close: set status to done
                        try:
                            auto_key = resolve_session_key()
                            completed = complete_task_document(prev_file, auto_key)
                        except (OSError, TaskDocumentError) as exc:
                            print(f"Error: cannot auto-close {prev_file}: {exc}", file=sys.stderr)
                            sys.exit(1)
                        _append_task_chronology(
                            agent_dir,
                            auto_key,
                            f"T{prev_task}:release",
                            f"auto-completed task {prev_task}",
                            len(completed.sessions),
                            "auto",
                            prev_file.stat().st_mtime_ns,
                        )
                        print(f"Auto-closed task {prev_task} (all gates checked).")
                elif prev_status != "done" and not force:
                    # prev task still has open gates — don't silently abandon it.
                    _gate_bounce(prev_task, prev_file, f"switching to task {task_num}")
                    sys.exit(1)
                elif prev_status != "done":
                    print(f"--force: switching away from task {prev_task} with open gates (left in_progress).")

        # Expand stubs on activation
        task_content = task_file.read_text(encoding="utf-8")
        import re as _stub_re
        stub_match = _stub_re.search(r'<!-- stub:(\w+) -->', task_content)
        if stub_match:
            stub_original = task_content
            stub_type = stub_match.group(1)
            # Extract user's Intent and Why sections before expanding
            def _extract_section(content, heading):
                pattern = rf'^## {heading}\n(.*?)(?=\n## |\Z)'
                m = _stub_re.search(pattern, content, _stub_re.MULTILINE | _stub_re.DOTALL)
                return m.group(1).strip() if m else ""

            user_intent = _extract_section(task_content, "Intent")
            user_why = _extract_section(task_content, "Why")
            user_refs = _extract_section(task_content, "References")

            # Render full template
            from tasks.template import render_template
            task_num_int = int(task_num)
            title = task_file.parent.name.split("-", 1)[1].replace("-", " ").title()
            full_content = render_template(num=task_num_int, title=title, task_type=stub_type)

            # F3: Append playbook role template (same as create_task)
            from tasks.core import _load_playbook
            role_template = _load_playbook(stub_type, project_path)
            if role_template:
                full_content += "\n" + role_template + "\n"

            # Inject preserved user content
            if user_intent:
                # F2: Try both placeholder variants (build + quick)
                for placeholder in [
                    "(what we want to achieve \u2014 the outcome, not the activity)",
                    "(one line \u2014 what to do and how to verify)",
                ]:
                    if placeholder in full_content:
                        full_content = full_content.replace(placeholder, user_intent)
                        break
            if user_why:
                full_content = full_content.replace(
                    "(why this matters now \u2014 urgency, context, what breaks if delayed)",
                    user_why,
                )
            # F1: Inject preserved references
            if user_refs and "(optional)" not in user_refs.lower():
                # Replace the default References content
                full_content = _stub_re.sub(
                    r'(## References\n).*?(?=\n---)',
                    f'## References\n{user_refs}',
                    full_content,
                    count=1,
                    flags=_stub_re.DOTALL,
                )

            try:
                replace_unclaimed_task_text(task_file, stub_original, full_content)
            except (OSError, TaskDocumentError) as exc:
                print(f"Error: cannot expand stub {task_file}: {exc}", file=sys.stderr)
                sys.exit(1)
            # Re-read for chat injection and display
            task_content = full_content
            print(f"Expanded stub to full {stub_type} template.")

        # The task document records successful claims; current_state is only
        # this session's navigation cache. The following gate makes the pair a
        # locked transaction and adds ownership refusal.
        try:
            claimed, claim_changed = claim_task_document(
                task_file,
                resolve_session_key(),
                expected_owner=expected_owner,
            )
        except TaskClaimConflict as exc:
            print(
                f"Error: task {task_num} is owned by "
                f"{exc.owner.provider}:{exc.owner.session_id}. "
                "Use --handoff-from or --recover-from with that exact owner; "
                "--force never steals a claim.",
                file=sys.stderr,
            )
            sys.exit(1)
        except (OSError, TaskDocumentError) as exc:
            print(f"Error: cannot activate {task_file}: {exc}", file=sys.stderr)
            sys.exit(1)
        if expected_owner is not None and claim_changed:
            print(
                f"Recorded explicit {transfer_kind} from "
                f"{expected_owner.provider}:{expected_owner.session_id}."
            )
        current_key = resolve_session_key()
        if claim_changed:
            if expected_owner is not None:
                action = transfer_kind or "handoff"
                message = (
                    f"{action} task {task_num} from "
                    f"{expected_owner.provider}:{expected_owner.session_id}"
                )
            elif candidate_document.status == "done":
                action = "reopen"
                message = f"reopened task {task_num}"
            else:
                action = "claim"
                message = f"claimed task {task_num}"
            _append_task_chronology(
                agent_dir,
                current_key,
                f"T{task_num}:{action}",
                message,
                len(claimed.sessions),
                expected_owner.provider if expected_owner else "-",
                expected_owner.session_id if expected_owner else "-",
                task_file.stat().st_mtime_ns,
            )
        try:
            write_navigation_cache(agent_dir, current_key, task_num)
            if expected_owner is not None and expected_owner != current_key:
                clear_navigation_cache(
                    agent_dir, expected_owner, expected_task=task_num
                )
        except (OSError, SessionStateError) as exc:
            print(
                f"Error: task {task_num} authority committed but navigation cache "
                f"reconciliation failed; rerun the same command: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

        # Workflow rules — deferred from bootstrap to task activation
        from tasks.template import workflow_briefing
        print("=== WORKFLOW ===")
        print(workflow_briefing())
        print()

        # Capture recent chat messages into task.md
        recent_chat = _capture_recent_chat(project_path)
        if recent_chat:
            original = task_file.read_text(encoding="utf-8")
            updated = _render_chat_into_task(original, recent_chat)
            if updated != original:
                try:
                    replace_claimed_task_text(
                        task_file, current_key, original, updated
                    )
                except (OSError, TaskDocumentError) as exc:
                    print(
                        f"Error: cannot attach recent chat to {task_file}: {exc}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            print(f"Captured {len(recent_chat)} recent chat message(s) into References.")

        # Print the full task file
        print(task_file.read_text(encoding="utf-8").rstrip())


    elif cmd == "new":
        # Parse --stub flag
        is_stub = False
        if cmd_args and cmd_args[0] == "--stub":
            is_stub = True
            cmd_args = cmd_args[1:]

        if len(cmd_args) < 2:
            print("Error: 'new' requires a type and a name", file=sys.stderr)
            print("Usage: pb-tasks new [--stub] <type> <name> [intent...]", file=sys.stderr)
            from tasks.core import list_all_types
            all_types = list_all_types(find_project_root())
            print(f"Types: {', '.join(all_types)}", file=sys.stderr)
            sys.exit(1)

        task_type = cmd_args[0]
        from tasks.core import list_all_types, _find_custom_playbook
        project_path_for_check = find_project_root()
        is_custom = _find_custom_playbook(project_path_for_check, task_type) is not None
        if task_type not in PLAYBOOKS and task_type != "quick" and not is_custom:
            all_types = list_all_types(project_path_for_check)
            print(f"Error: unknown type '{task_type}'", file=sys.stderr)
            print(f"Types: {', '.join(all_types)}", file=sys.stderr)
            sys.exit(1)

        # args[1] = name, args[2:] = optional intent text
        task_name = cmd_args[1]
        intent_text = " ".join(cmd_args[2:]) if len(cmd_args) > 2 else None
        project_path = find_project_root()

        # Check if user included a task number prefix
        import re as _re
        from tasks.core import _next_task_number
        num_match = _re.match(r'^(\d{3})-(.+)$', task_name)
        if num_match:
            provided_num = int(num_match.group(1))
            tasks_dir = resolve_agent_dir(project_path) / "tasks"
            next_num = _next_task_number(tasks_dir)
            if provided_num == next_num:
                # Matches next number - strip it (user was explicit)
                task_name = num_match.group(2)
            else:
                print(f"Error: provided task number {provided_num:03d} doesn't match next number {next_num:03d}", file=sys.stderr)
                print(f"Usage: pb-tasks new {task_type} {num_match.group(2)}", file=sys.stderr)
                sys.exit(1)
        task_file = create_task(project_path, task_name, task_type=task_type,
                               intent_text=intent_text, stub=is_stub)
        pattern_name = PLAYBOOKS.get(task_type, f"custom ({task_type})")

        import re
        task_num_match = re.match(r'^(\d+)-', task_file.parent.name)
        task_num = task_num_match.group(1) if task_num_match else "?"

        print(f"Created: {task_file.relative_to(project_path)}")
        if is_stub:
            print(f"Stub ({pattern_name}) — expand with: pb-tasks work {task_num}")
        elif task_type != "quick":
            print(f"Pattern: {pattern_name}")
            print(f"Next: fill in task.md gates, then run: pb-tasks work {task_num}")
        else:
            print(f"Next: fill in task.md gates, then run: pb-tasks work {task_num}")
        print()

        if task_type != "quick":
            # Print full playbook so agent has workflow guidance inline
            playbook_path = _find_playbook_skill(project_path)
            if playbook_path:
                playbook_file = Path(playbook_path)
                if playbook_file.exists():
                    print("=== PLAYBOOK (task.md design guide) ===")
                    print("Use this to improve your task.md: select patterns and gates as appropriate,")
                    print("or invent new ones. This is a starting point — expand as needed.")
                    print()
                    content = playbook_file.read_text(encoding="utf-8")
                    # Strip sections not relevant to task design
                    for marker in ["## Mind Map", "> Evidence base:"]:
                        idx = content.find(marker)
                        if idx > 0:
                            content = content[:idx]
                    print(content.rstrip())
                    print()
                    print(f"Now fill in {task_file.relative_to(project_path)} — design a good task.md.")

    elif cmd == "init":
        status = _run_init(cmd_args)
        if status:
            sys.exit(status)

    elif cmd == "bootstrap":
        project_path = find_project_root()

        # A bare provider launch reaches bootstrap with its native identity even
        # when no wrapper ran. Validate/create the same durable skeleton hooks
        # use, and enrich only descriptive restart context.
        try:
            key = resolve_session_key()
        except NativeSessionIdentityError as exc:
            identity_markers = (
                "PLAYBOOK_PROVIDER",
                "PLAYBOOK_BRIDGE_PROVIDER",
                "CLAUDE_CODE_SESSION_ID",
                "CODEX_THREAD_ID",
                "ANTIGRAVITY_CONVERSATION_ID",
            )
            if any(os.environ.get(name) for name in identity_markers):
                print(f"Error: cannot bootstrap Playbook session: {exc}", file=sys.stderr)
                sys.exit(1)
            key = None
        if key is not None:
            agent_dir = resolve_agent_dir(project_path)
            _record_path, session_record = ensure_session_record(
                agent_dir,
                key.provider,
                key.session_id,
                enrich={
                    "project": str(project_path.resolve()),
                    "resume_cwd": str(Path.cwd().resolve()),
                },
            )
            if session_record.get("managed") is not True:
                from datetime import datetime, timezone
                from tasks.chat_state import (
                    append_chat_event,
                    derive_event_key,
                )
                try:
                    created = datetime.fromisoformat(
                        session_record["created_at"].replace("Z", "+00:00")
                    ).astimezone(timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S UTC"
                    )
                    append_chat_event(
                        agent_dir,
                        "S:discover",
                        key.provider,
                        key.session_id,
                        created,
                        "discovered ad-hoc provider session during bootstrap",
                        event_key=derive_event_key(
                            "S:discover", key.provider, key.session_id
                        ),
                    )
                except (OSError, ValueError) as exc:
                    print(
                        "Warning: session bootstrap succeeded but chat chronology "
                        f"could not be appended: {exc}",
                        file=sys.stderr,
                    )
        else:
            agent_dir = resolve_agent_dir(project_path)

        # Identity preamble
        from tasks.template import identity_preamble, mind_map_header
        print(identity_preamble())
        try:
            runtime_surface, _runtime_matches = runtime_generation_status(project_path)
            print(runtime_surface)
        except RuntimeError as exc:
            print(f"Playbook runtime: UNSAFE — {exc}")
        if key is not None:
            print(_session_surface(agent_dir, key))
            print(
                "Native identity authority: the Session line above names this process; "
                "task.md ## Sessions entries are ownership history, not your current identity."
            )
        else:
            print("Session: unavailable (read-only bootstrap; no native identity)")
        try:
            print(_session_store_surface(agent_dir))
        except (OSError, SessionStateError) as exc:
            print(f"Session store: UNSAFE — {exc}")
        print()

        # Mind Map — full dump with navigation header
        mm_content = _load_mind_map(project_path)
        if mm_content:
            print("=== MIND MAP (MIND_MAP.md) ===")
            print(mind_map_header())
            print()
            print(mm_content.rstrip())
            print()

        skills = _playbook_skill_catalog()
        if skills:
            print("=== PLAYBOOK SKILLS ===")
            print(
                "Provider integrations install the full skill; compatible agents load it "
                "when its trigger matches."
            )
            for name, description in skills:
                summary = description if len(description) <= 180 else description[:177] + "..."
                print(f"  {name:<16} {summary}")
            print()

        # Pending tasks
        print("=== PENDING TASKS ===")
        list_tasks(project_path, pending_only=True)

        # A bounded lineage window supports the onboarding requirement to
        # inspect recent work before creating a task without dumping history.
        print()
        print("=== RECENT TASKS (LATEST 3) ===")
        list_tasks(project_path, recent_only=True)

        print()
        print("=== RECENT HUMAN MESSAGES (LATEST 50; CONTEXT ONLY) ===")
        chat_log = agent_dir / "chat_log.md"
        if chat_log.is_file():
            recent_messages = _compact_chat_lines(chat_log, last_n=50, width=300)
            if recent_messages:
                print("\n".join(recent_messages))
            else:
                print("(no human messages recorded)")
        else:
            print("(no chat_log.md yet)")
        print(
            "Context is orientation, not authorization. Expand with: "
            "pb-tasks log <N> [--width <W>]; task-specific: pb-tasks context <N>"
        )

        # CLI reference — shown last so mind map + tasks aren't buried
        from tasks.template import cli_reference
        print()
        print("=== CLI REFERENCE ===")
        print(cli_reference())

    elif cmd in ("list", "ls"):
        project_path = find_project_root()
        pending_only = "--pending" in cmd_args
        recent_only = "--recent" in cmd_args
        if pending_only and recent_only:
            print("Error: choose either --pending or --recent", file=sys.stderr)
            sys.exit(1)
        list_tasks(
            project_path,
            pending_only=pending_only,
            recent_only=recent_only,
        )

    elif cmd == "panel-review":
        from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeout

        # Parse flags
        review_mode = "plan"
        web_search = False
        timeout_secs = 300  # 5 min default
        extra_prompt = ""
        no_mind_map = False
        bare = False
        models_flag = None  # --models CSV → explicit judge set for this run
        remaining_args = []
        i = 0
        while i < len(cmd_args):
            if cmd_args[i] == "--mode" and i + 1 < len(cmd_args):
                review_mode = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--models" and i + 1 < len(cmd_args):
                models_flag = [s.strip() for s in cmd_args[i + 1].split(",") if s.strip()]
                i += 2
            elif cmd_args[i] == "--web-search":
                web_search = True
                i += 1
            elif cmd_args[i] == "--timeout" and i + 1 < len(cmd_args):
                timeout_secs = int(cmd_args[i + 1])
                i += 2
            elif cmd_args[i] == "--prompt" and i + 1 < len(cmd_args):
                extra_prompt = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--no-mind-map":
                no_mind_map = True
                i += 1
            elif cmd_args[i] == "--bare":
                bare = True
                i += 1
            else:
                remaining_args.append(cmd_args[i])
                i += 1

        if review_mode not in ("plan", "impl"):
            print(f"Error: unknown mode '{review_mode}'", file=sys.stderr)
            sys.exit(1)

        task_num = remaining_args[0] if remaining_args else ""
        if task_num.isdigit():
            task_num = task_num.zfill(3)

        # Task number is optional; --prompt required when omitted
        if not task_num and not extra_prompt:
            print("Error: 'panel-review' requires a task number or --prompt", file=sys.stderr)
            print("Usage: pb-tasks panel-review [<number>] [--mode plan|impl] [--models codex:gpt-5.5,agy,...] [--prompt \"...\"] [--no-mind-map] [--bare] [--web-search] [--timeout SECONDS]", file=sys.stderr)
            sys.exit(1)

        project_path = find_project_root()

        # Resolve task file if task number given
        task_file = None
        task_path = None
        if task_num:
            tasks_dir = resolve_agent_dir(project_path) / "tasks"
            matches = list(tasks_dir.glob(f"{task_num}-*/task.md"))
            if not matches:
                print(f"Task {task_num} not found", file=sys.stderr)
                sys.exit(1)
            task_file = matches[0]
            task_path = str(task_file.relative_to(project_path))

        from tasks.template import panel_plan_review_prompt, panel_impl_review_prompt

        # Build isolated judge context. Taskless panels get project memory plus
        # the explicit prompt, never an ambient raw chat tail.
        MAX_CONTEXT_CHARS = 100_000
        system_context = "" if bare else _build_judge_context(
            project_path,
            task_file=task_file,
            task_path=task_path,
            include_mind_map=not no_mind_map,
            max_chars=MAX_CONTEXT_CHARS,
        )

        # Prompt strategy: bare/taskless → extra_prompt is full mission; with task → review prompt + optional steering
        if task_file:
            prompt_fn = panel_plan_review_prompt if review_mode == "plan" else panel_impl_review_prompt
            review_label = "plan review" if review_mode == "plan" else "impl review"
        else:
            prompt_fn = None
            review_label = "panel"

        # Output path: task dir when task given, agent_dir/ otherwise
        if task_file:
            judge_md = task_file.parent / "judge.md"
        else:
            agent_dir = resolve_agent_dir(project_path)
            agent_dir.mkdir(exist_ok=True)
            judge_md = agent_dir / "judge.md"

        # Discover available judges via adapter classes — each adapter declares
        # its own binary_name() and panel_variants(). Adding a new provider is
        # a one-line append to PANEL_ADAPTERS; no dispatch changes needed.
        from provider.adapters.claude import ClaudeAdapter
        from provider.adapters.codex import CodexAdapter
        from provider.adapters.antigravity import AntigravityAdapter
        from provider.adapters.pi import PiAdapter
        from provider.sandbox import load_judge_config, resolve_judge_spec
        PANEL_ADAPTERS = (ClaudeAdapter, CodexAdapter, AntigravityAdapter, PiAdapter)
        _JUDGE_ADAPTERS = {
            "claude": ClaudeAdapter, "codex": CodexAdapter,
            "agy": AntigravityAdapter, "pi": PiAdapter,
        }

        # Judge-set precedence: --models flag → models.json `panel` (shipped ⊕
        # project .agent/models.json) → legacy full fan-out (only if no config).
        if models_flag is not None:
            spec_names = models_flag
        else:
            spec_names = load_judge_config().get("panel") or None

        judges = []  # list of (adapter_cls, variant)
        if spec_names:
            skipped = []
            for nm in spec_names:
                try:
                    provider, variant = resolve_judge_spec(nm)
                except ValueError as e:
                    print(f"Error: {e}", file=sys.stderr)
                    sys.exit(1)
                cls = _JUDGE_ADAPTERS.get(provider)
                if cls is None:
                    print(f"Error: no adapter for provider '{provider}' (spec '{nm}')", file=sys.stderr)
                    sys.exit(1)
                if cls.is_available():
                    judges.append((cls, variant))
                else:
                    skipped.append(f"{nm} ({cls.binary_name()} not on PATH)")
            if skipped:
                print(f"  Skipped unavailable: {', '.join(skipped)}", flush=True)
        else:
            # No configured panel — legacy discovery (all providers × variants).
            for cls in PANEL_ADAPTERS:
                if cls.is_available():
                    for variant in cls.panel_variants():
                        judges.append((cls, variant))

        if not judges:
            print("Error: no available judges. Install a provider CLI, or name "
                  "reachable ones with --models (e.g. --models codex:gpt-5.5,agy).",
                  file=sys.stderr)
            sys.exit(1)

        display_target = task_path or "(promptless)"
        print(f"Running panel {review_label} on {display_target} ({len(judges)} judges, {timeout_secs}s timeout)...", flush=True)

        def run_judge(judge_spec):
            adapter_cls, variant = judge_spec
            provider_name = adapter_cls.binary_name()
            label = f"{provider_name}:{variant}" if variant else provider_name
            if prompt_fn:
                prompt = prompt_fn(task_path, inline_context=(provider_name != "claude"))
                if extra_prompt:
                    prompt += f"\n\nAdditional steering from the user:\n{extra_prompt}"
            else:
                prompt = extra_prompt

            try:
                adapter = adapter_cls(session_id="judge", project_root=project_path)
                output = adapter.run_headless_judge(
                    prompt=prompt,
                    model=variant,
                    system_context=system_context,
                    web_search=web_search,
                    timeout_secs=timeout_secs,
                )
                return label, output
            except subprocess.TimeoutExpired:
                return label, f"(timed out after {timeout_secs}s)"
            except Exception as e:
                return label, f"(error: {e})"

        # Run all judges in parallel
        import concurrent.futures
        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(judges)) as executor:
            futures = {executor.submit(run_judge, j): j for j in judges}
            for future in concurrent.futures.as_completed(futures):
                label, output = future.result()
                results[label] = output
                print(f"  [{label}] done", flush=True)

        # Classify each judge as succeeded vs failed — a failed judge must NOT
        # read as a clean empty review (T139). Failure markers come from the
        # adapters' format_judge_output ("(FAILED — exit N)", "(no output)") and
        # run_judge's own guards ("(timed out…)", "(error:…)").
        def _judge_failed(text: str) -> bool:
            t = text.lstrip()
            return (t.startswith("(FAILED") or t.startswith("(timed out")
                    or t.startswith("(error") or t == "(no output)")

        failed = {lbl for lbl, out in results.items() if _judge_failed(out)}
        succeeded = len(results) - len(failed)

        # Write judge.md (path already set above based on task_file presence)
        display_label = task_path or extra_prompt[:60]
        lines = [f"# Panel {review_label.title()} — {display_label}\n"]
        lines.append(f"**Judges:** {succeeded}/{len(results)} succeeded | **Web search:** {'yes' if web_search else 'no'} | **Timeout:** {timeout_secs}s\n")
        if failed:
            lines.append(f"**⚠ Failed judges:** {', '.join(sorted(failed))} — see their blocks below for the exit code / stderr. NOT a clean empty review.\n")
        lines.append("\n")
        # Triage frame (T124): prepend the pushback discipline AT THE TOP so
        # the reading agent meets the instruction BEFORE the per-judge
        # findings — primes the triage lens before the data is read.
        # The judges themselves never see this; it's bundled with their
        # outputs purely for the reading agent. Mirrors the in-task pushback
        # gate from template.judge_section / judge_impl_section, but for
        # panel reviews (where findings live in judge.md, not task.md) the
        # discipline rides with the data. Helper is unit-tested in tests/test_cli.py.
        lines.extend(_panel_triage_frame())
        for label in sorted(results.keys()):
            tag = "  [FAILED]" if label in failed else ""
            lines.append("═" * 60)
            lines.append(f"  JUDGE: {label}{tag}")
            lines.append("═" * 60 + "\n")
            lines.append(results[label].strip())
            lines.append("\n\n")
        judge_md.write_text("\n".join(lines), encoding="utf-8")
        summary = f"\nSaved: {judge_md.relative_to(project_path)} ({succeeded}/{len(judges)} judges succeeded)"
        if failed:
            summary += f"; FAILED: {', '.join(sorted(failed))}"
        print(summary, flush=True)

    elif cmd in ("plan-review", "impl-review", "judge"):
        # "judge" is a legacy alias — auto-detects mode from task status
        review_cmd = cmd
        if not cmd_args:
            print(f"Error: '{review_cmd}' requires a task number", file=sys.stderr)
            print(f"Usage: tasks {review_cmd} <number> [--backend codex|claude|agy|pi] [--model <variant>] [--prompt \"...\"]  (default backend: models.json default_judge, ships codex)", file=sys.stderr)
            sys.exit(1)

        # Parse flags
        backend = None   # explicit --backend; else from models.json default_judge
        model = None     # explicit --model (variant within the backend)
        extra_prompt = ""
        remaining_args = []
        i = 0
        while i < len(cmd_args):
            if cmd_args[i] == "--backend" and i + 1 < len(cmd_args):
                backend = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--model" and i + 1 < len(cmd_args):
                model = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--prompt" and i + 1 < len(cmd_args):
                extra_prompt = cmd_args[i + 1]
                i += 2
            else:
                remaining_args.append(cmd_args[i])
                i += 1

        # No --backend → models.json default_judge (provider or provider:variant,
        # project-overridable; ships as "codex" so headless review avoids the
        # metered claude -p path by default). --model overrides the variant.
        if backend is None:
            from provider.sandbox import load_judge_config, resolve_judge_spec
            dj = load_judge_config().get("default_judge") or "claude"
            try:
                backend, dj_variant = resolve_judge_spec(dj)
            except ValueError:
                backend, dj_variant = dj, None
            if model is None:
                model = dj_variant

        # Accept friendlier aliases: "agy"/"gemini" → "antigravity", "qwen" → "pi"
        if backend in ("agy", "gemini"):
            backend = "antigravity"
        elif backend == "qwen":
            backend = "pi"
        if backend not in ("claude", "codex", "antigravity", "pi"):
            print(f"Error: unknown backend '{backend}'", file=sys.stderr)
            print("Supported: codex (default), claude, antigravity (alias: agy), pi (alias: qwen)", file=sys.stderr)
            sys.exit(1)

        if not remaining_args:
            print(f"Error: '{review_cmd}' requires a task number", file=sys.stderr)
            sys.exit(1)

        task_num = remaining_args[0]
        if task_num.isdigit():
            task_num = task_num.zfill(3)
        project_path = find_project_root()
        tasks_dir = resolve_agent_dir(project_path) / "tasks"
        matches = list(tasks_dir.glob(f"{task_num}-*/task.md"))
        if not matches:
            print(f"Task {task_num} not found", file=sys.stderr)
            sys.exit(1)

        task_file = matches[0]
        task_path = str(task_file.relative_to(project_path))

        from tasks.template import plan_review_prompt, impl_review_prompt

        # Build isolated context: mind map + selected task only.
        MAX_CONTEXT_CHARS = 100_000
        system_context = _build_judge_context(
            project_path,
            task_file=task_file,
            task_path=task_path,
            max_chars=MAX_CONTEXT_CHARS,
        )

        # Determine mode: explicit from command, or auto-detect for legacy "judge"
        if review_cmd == "plan-review":
            review_mode = "plan"
        elif review_cmd == "impl-review":
            review_mode = "impl"
        else:  # legacy "judge" — auto-detect from status
            from tasks.core import _extract_status
            review_mode = "impl" if _extract_status(task_file) == "done" else "plan"

        prompt_fn = plan_review_prompt if review_mode == "plan" else impl_review_prompt
        review_label = "plan review" if review_mode == "plan" else "impl review"

        if backend == "claude":
            claude_bin = shutil.which("claude")
            if not claude_bin:
                print("Error: 'claude' not found on PATH", file=sys.stderr)
                sys.exit(1)

            prompt = prompt_fn(task_path)
            if extra_prompt:
                prompt += f"\n\nAdditional steering from the user:\n{extra_prompt}"
            env = os.environ.copy()
            env["CLAUDECODE"] = ""
            env.pop("CLAUDE_CODE_SSE_PORT", None)
            env.pop("CLAUDE_CODE_ENTRYPOINT", None)
            env["PLAYBOOK_SESSION_ID"] = "judge"
            env["PLAYBOOK_ROLE"] = "noninteractive"

            # Bypass flag injected by provider.sandbox.run() — don't pass here.
            # The judge is a read-only evaluator sandboxed via provider.sandbox
            # (write containment via seatbelt/bwrap). PLAYBOOK_SESSION_ID=judge
            # above lets hooks identify judge sessions if needed.
            claude_args = ["-p", "--max-budget-usd", "2"]
            if model:
                from provider.adapters.claude import ClaudeAdapter
                claude_args += ["--model", ClaudeAdapter._MODEL_MAP.get(model, model)]
            claude_args += ["--append-system-prompt", system_context, prompt]

            from provider import sandbox as _sandbox
            print(f"Running {review_label} (claude) on {task_path}...", flush=True)
            result = _sandbox.run(
                "claude",
                claude_args,
                project_root=project_path,
                env=env,
                capture_output=True,
                text=True,
            )

        elif backend == "codex":
            if not shutil.which("codex"):
                print("Error: 'codex' not found on PATH", file=sys.stderr)
                print("Install: https://github.com/openai/codex", file=sys.stderr)
                sys.exit(1)

            prompt = prompt_fn(task_path, inline_context=True)
            # Codex has no system prompt — inline context into the user prompt
            full_prompt = f"{system_context}\n\n---\n\n{prompt}"

            codex_log = task_file.parent / "judge-codex.log"
            # Bypass flag (--dangerously-bypass-approvals-and-sandbox) inserted
            # after `exec` by provider.sandbox._compose_agent_argv.
            codex_args = ["exec"]
            if model:
                codex_args += ["-m", model]
            codex_args += [
                "-s", "workspace-write",
                "--ephemeral",
                "-C", str(project_path),
                "-o", str(codex_log),
                "-",  # read prompt from stdin
            ]

            codex_env = os.environ.copy()
            codex_env["PLAYBOOK_SESSION_ID"] = "judge"
            codex_env["PLAYBOOK_ROLE"] = "noninteractive"

            from provider import sandbox as _sandbox
            print(f"Running {review_label} (codex) on {task_path}...", flush=True)
            result = _sandbox.run(
                "codex", codex_args,
                project_root=project_path,
                env=codex_env,
                input=full_prompt,
                capture_output=True,
                text=True,
            )

        elif backend == "antigravity":  # agy
            if not shutil.which("agy"):
                print("Error: 'agy' not found on PATH", file=sys.stderr)
                sys.exit(1)

            prompt = prompt_fn(task_path, inline_context=True)
            full_prompt = f"{system_context}\n\n---\n\n{prompt}"
            if extra_prompt:
                full_prompt += f"\n\nAdditional steering from the user:\n{extra_prompt}"

            # agy --print mode ignores cwd, so it needs --add-dir.
            # Bypass (--dangerously-skip-permissions) prepended by sandbox.
            agy_args = ["--add-dir", str(project_path)]
            if model:
                agy_args += ["--model", model]
            agy_args += ["--print", full_prompt, "--print-timeout", "300s"]

            agy_env = os.environ.copy()
            agy_env["PLAYBOOK_SESSION_ID"] = "judge"
            agy_env["PLAYBOOK_ROLE"] = "noninteractive"

            from provider import sandbox as _sandbox
            print(f"Running {review_label} (agy) on {task_path}...", flush=True)
            result = _sandbox.run(
                "agy", agy_args,
                project_root=project_path,
                env=agy_env,
                capture_output=True,
                text=True,
            )

        else:  # pi (local Qwen via oMLX)
            if not (shutil.which("pi") or shutil.which("omlx")):
                print("Error: neither 'pi' nor 'omlx' found on PATH", file=sys.stderr)
                print("Install: oMLX app (https://omlx.app/) or pi CLI", file=sys.stderr)
                sys.exit(1)

            prompt = prompt_fn(task_path, inline_context=True)

            # Pi has no system prompt convention — append-system-prompt threads
            # the system context. --no-context-files skips AGENTS.md/CLAUDE.md
            # auto-load so the judge isn't biased by project conventions.
            # --provider oss points at the local oMLX endpoint (127.0.0.1:8000).
            pi_args = [
                "-p", prompt,
                "--provider", "oss",
                "--no-context-files",
                "--append-system-prompt", system_context,
            ]
            if model:
                pi_args += ["--model", model]

            pi_env = os.environ.copy()
            pi_env["PLAYBOOK_SESSION_ID"] = "judge"
            pi_env["PLAYBOOK_ROLE"] = "noninteractive"

            from provider import sandbox as _sandbox
            print(f"Running {review_label} (pi) on {task_path}...", flush=True)
            result = _sandbox.run(
                "pi", pi_args,
                project_root=project_path,
                env=pi_env,
                capture_output=True,
                text=True,
            )

        if result.stdout:
            print(result.stdout, end="", flush=True)
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr, flush=True)

        # Save bounded review evidence outside task.md. The independent judge
        # never owns the task; the invoking owner verifies and publishes any
        # accepted findings through the task authority path.
        MAX_REVIEW_ARTIFACT_CHARS = 50_000
        log_name = {
            "claude": "judge.log",
            "codex": "judge-codex.log",
            "antigravity": "judge-agy.log",
            "pi": "judge-pi.log",
        }.get(backend, "judge.log")
        judge_log = task_file.parent / log_name
        if backend == "codex" and judge_log.is_file():
            output = judge_log.read_text(encoding="utf-8").strip()
        else:
            output = (result.stdout or "").strip()
        if result.returncode != 0 and not output:
            if judge_log.exists():
                print(f"\nReview failed (exit {result.returncode}); kept previous {judge_log.relative_to(project_path)}", flush=True)
            else:
                print(f"\nReview failed (exit {result.returncode}); no output to save", flush=True)
        else:
            if len(output) > MAX_REVIEW_ARTIFACT_CHARS:
                output = (
                    output[:MAX_REVIEW_ARTIFACT_CHARS]
                    + "\n\n[... review artifact truncated by Playbook ...]\n"
                )
            judge_log.write_text(output + ("\n" if output else ""), encoding="utf-8")
            print(f"\nSaved: {judge_log.relative_to(project_path)}", flush=True)
            print(
                "Owning session: verify and triage this artifact, then publish "
                "accepted findings in the exact current task.md gate using "
                "your normal structured file-edit tool.",
                flush=True,
            )

        sys.exit(result.returncode)

    elif cmd == "context":
        if not cmd_args:
            print("Error: 'context' requires a task number", file=sys.stderr)
            print("Usage: pb-tasks context <number>", file=sys.stderr)
            sys.exit(1)

        task_num = cmd_args[0]
        if task_num.isdigit():
            task_num = task_num.zfill(3)
        project_path = find_project_root()

        chat_log = resolve_agent_dir(project_path) / "chat_log.md"
        if not chat_log.exists():
            print("No .agent/chat_log.md found.", file=sys.stderr)
            sys.exit(1)

        import re
        open_tag = re.compile(r'^<!--\s*T' + re.escape(task_num) + r'\s*-->$')
        close_tag = re.compile(r'^<!--\s*/T' + re.escape(task_num) + r'\s*-->$')

        spans = []
        current_span = []
        inside = False
        for line in chat_log.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not inside and open_tag.match(stripped):
                inside = True
                continue
            elif inside and close_tag.match(stripped):
                spans.append("\n".join(current_span))
                current_span = []
                inside = False
                continue
            if inside:
                current_span.append(line)

        # Handle unclosed span at end of file
        if inside and current_span:
            spans.append("\n".join(current_span))

        if not spans:
            print(f"No attributed messages for task {task_num}.", file=sys.stderr)
            sys.exit(1)

        # Token-efficient output: user messages only; typed Playbook events are
        # chronology, not user intent.
        from tasks.chat_state import parse_chat_entries
        max_line = 200
        for span in spans:
            for entry in parse_chat_entries(span):
                if not entry.marker.startswith("M"):
                    continue
                text = " ".join(entry.body.split())
                if len(text) > max_line:
                    text = text[:max_line] + "..."
                label = f" {entry.session_name}" if entry.session_name else ""
                identity = (
                    f" ({entry.provider}/{entry.session_id})"
                    if entry.provider and entry.session_id else ""
                )
                print(f"[{entry.marker}]{label}{identity} {text}")

    elif cmd == "intent":
        # Vertical retro: 4 blind intent extractions over one task's layers.
        if not cmd_args:
            print("Error: 'intent' requires a task number", file=sys.stderr)
            print("Usage: tasks intent <number> [--chat-file P] [--base REF --head REF] "
                  "[--collect-only] [--timeout S]", file=sys.stderr)
            sys.exit(1)

        task_num = cmd_args[0]
        if task_num.isdigit():
            task_num = task_num.zfill(3)
        chat_file = base = head = None
        collect_only = False
        timeout_secs = 300
        i = 1
        while i < len(cmd_args):
            a = cmd_args[i]
            if a == "--chat-file" and i + 1 < len(cmd_args):
                chat_file = Path(cmd_args[i + 1]); i += 2
            elif a == "--base" and i + 1 < len(cmd_args):
                base = cmd_args[i + 1]; i += 2
            elif a == "--head" and i + 1 < len(cmd_args):
                head = cmd_args[i + 1]; i += 2
            elif a == "--collect-only":
                collect_only = True; i += 1
            elif a == "--timeout" and i + 1 < len(cmd_args):
                timeout_secs = int(cmd_args[i + 1]); i += 2
            else:
                print(f"Error: unknown option for intent: {a}", file=sys.stderr)
                sys.exit(1)

        if bool(base) != bool(head):
            print("Error: --base and --head must be given together (an explicit range)",
                  file=sys.stderr)
            sys.exit(1)

        from tasks.intent import (
            collect_all, run_extractions, make_default_runner,
            write_run, find_task_dir, new_run_id, last_intent_entry, LAYERS,
        )
        project_path = find_project_root()
        agent_dir = resolve_agent_dir(project_path)
        task_dir = find_task_dir(agent_dir / "tasks", task_num)
        if task_dir is None:
            print(f"Error: no task {task_num} under {agent_dir / 'tasks'}", file=sys.stderr)
            sys.exit(1)

        slices = collect_all(project_path, agent_dir, task_dir, task_num,
                             chat_file=chat_file, base=base, head=head)
        print(f"Intent review — task {task_num} ({task_dir.name})")
        for layer in LAYERS:
            s = slices[layer]
            print(f"  {layer:7} {'✓' if s.available else '✗'}  {s.provenance}")
        avail = [l for l in LAYERS if slices[l].available]
        if not avail:
            print("Error: no available evidence on any layer — nothing to infer. "
                  "Pass --chat-file and/or --base/--head.", file=sys.stderr)
            sys.exit(1)

        run_id = new_run_id()
        if collect_only:
            from tasks.intent import build_prompt
            reports = {l: (build_prompt(slices[l]) if slices[l].available
                           else f"# Intent inferred from {l}\n\n_(no evidence — "
                                f"{slices[l].provenance})_\n") for l in LAYERS}
            print("\n(--collect-only: wrote prompts, skipped model calls)")
        else:
            print(f"\nRunning {len(avail)} blind extraction(s) "
                  f"(default judge, {timeout_secs}s each)...", flush=True)
            reports = run_extractions(slices, make_default_runner(
                project_path, timeout_secs=timeout_secs))

        run_dir = write_run(task_dir, slices, reports, run_id=run_id)
        rel = run_dir.relative_to(project_path)
        print(f"\nReports written: {rel}/")
        print(f"Grading sheet:   {rel}/review.md")
        prior = last_intent_entry(project_path / "INTENT.md", task_num)
        if prior:
            print("Prior validated intent exists — reconcile as a DELTA against INTENT.md.")
        print("\nNext: read review.md with the user, grade the seams, then append "
              "vetted intent to INTENT.md (the /intent command drives this).")

    elif cmd == "timeline":
        project_path = find_project_root()
        bash_history = resolve_agent_dir(project_path) / "bash_history"
        if not bash_history.exists():
            print("No .agent/bash_history found.", file=sys.stderr)
            sys.exit(1)

        import re
        # Match: timestamp | AGENT/SCRIPT | tasks work/new/done ...
        pattern = re.compile(
            r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| \w+ \| '
            r'(?:.*/)?(tasks (?:work|new) .+)$'
        )
        seen = set()
        for line in bash_history.read_text(encoding="utf-8").splitlines():
            m = pattern.match(line)
            if m:
                cmd = m.group(2)
                # Deduplicate AGENT+SCRIPT echoes (same command within 2 lines)
                if cmd not in seen:
                    seen.add(cmd)
                    print(f"{m.group(1)}  {cmd}")
                else:
                    seen.discard(cmd)

    elif cmd == "tagger":
        project_path = find_project_root()
        chat_log = resolve_agent_dir(project_path) / "chat_log.md"
        bash_history = resolve_agent_dir(project_path) / "bash_history"
        if not chat_log.exists():
            print("No .agent/chat_log.md found.", file=sys.stderr)
            sys.exit(1)
        if not bash_history.exists():
            print("No .agent/bash_history found.", file=sys.stderr)
            sys.exit(1)

        import re

        # 1. Parse only user messages; G/T/S rows are chronology, not intent.
        from tasks.chat_state import parse_chat_entries
        entries = []  # (timestamp_str, sort_key, display_line)
        max_line = 200
        for entry in parse_chat_entries(chat_log.read_text(encoding="utf-8")):
            if not entry.marker.startswith("M"):
                continue
            text = " ".join(entry.body.split())
            if len(text) > max_line:
                text = text[:max_line] + "..."
            entries.append(
                (
                    entry.timestamp.removesuffix(" UTC"),
                    0,
                    f"[{entry.marker}] {text}",
                )
            )

        # 2. Parse task transitions from bash_history
        task_pattern = re.compile(
            r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| \w+ \| '
            r'(?:.*/)?(tasks (?:work|new) .+)$'
        )
        seen = set()
        for line in bash_history.read_text(encoding="utf-8").splitlines():
            m = task_pattern.match(line)
            if m:
                task_cmd = m.group(2)
                if task_cmd not in seen:
                    seen.add(task_cmd)
                    entries.append((m.group(1), 1, f"--- {task_cmd} ---"))
                else:
                    seen.discard(task_cmd)

        # 3. Sort by timestamp, then task transitions before messages (sort_key: 1 before 0)
        #    Actually: task transitions AFTER messages at same timestamp makes more sense
        #    But transitions should come BEFORE subsequent messages — sort_key 1 means
        #    transitions sort after messages at same second. That's fine: the transition
        #    happened between messages.
        entries.sort(key=lambda e: (e[0], e[1]))

        # 4. Output
        for _, _, display in entries:
            print(display)

    elif cmd == "tag":
        dry_run = "--dry-run" in cmd_args
        project_path = find_project_root()
        chat_log = resolve_agent_dir(project_path) / "chat_log.md"
        bash_history = resolve_agent_dir(project_path) / "bash_history"
        if not chat_log.exists():
            print("No .agent/chat_log.md found.", file=sys.stderr)
            sys.exit(1)
        if not bash_history.exists():
            print("No .agent/bash_history found.", file=sys.stderr)
            sys.exit(1)

        import re
        from bisect import bisect_right

        # 1. Build sorted task transition list from bash_history
        #    Each entry: (timestamp, active_task_or_None)
        task_pattern = re.compile(
            r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| \w+ \| '
            r'(?:.*/)?(tasks (?:work|new) .+)$'
        )
        work_re = re.compile(r'tasks work (\d+)')
        transitions = []  # [(timestamp, task_num_or_None)]
        seen = set()
        for line in bash_history.read_text(encoding="utf-8").splitlines():
            m = task_pattern.match(line)
            if m:
                task_cmd = m.group(2)
                if task_cmd not in seen:
                    seen.add(task_cmd)
                else:
                    seen.discard(task_cmd)
                    continue
                ts = m.group(1)
                if "work done" in task_cmd:
                    transitions.append((ts, None))
                else:
                    wm = work_re.search(task_cmd)
                    if wm:
                        transitions.append((ts, wm.group(1).zfill(3)))
        transitions.sort(key=lambda t: t[0])
        trans_times = [t[0] for t in transitions]

        def active_task_at(ts):
            """Return task number active at timestamp ts, or None."""
            idx = bisect_right(trans_times, ts) - 1
            if idx < 0:
                return None
            return transitions[idx][1]

        # 2. Scan chat_log.md, find message headers with timestamps,
        #    insert tags at task transition points
        msg_header = re.compile(
            r'^(\*\*\[(M\d+)\]\*\* \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\])'
        )
        # Also detect existing tags to avoid double-tagging
        existing_tag = re.compile(r'^<!--\s*/?T\d+\s*-->$')

        from tasks.chat_state import chat_log_lock
        with chat_log_lock(chat_log.parent):
            chat_before = chat_log.read_bytes()
        lines = chat_before.decode("utf-8").splitlines(keepends=True)
        output = []
        current_tag = None  # currently open tag (task number)
        tags_inserted = 0

        for line in lines:
            stripped = line.strip()
            # Skip existing attribution tags (we'll rewrite them)
            if existing_tag.match(stripped):
                continue

            m = msg_header.match(stripped)
            if m:
                msg_id = m.group(2)
                msg_ts = m.group(3)
                task = active_task_at(msg_ts)

                if task != current_tag:
                    # Close previous tag if open
                    if current_tag is not None:
                        output.append(f"<!-- /T{current_tag} -->\n")
                        output.append("\n")
                        tags_inserted += 1
                    # Open new tag if task is active
                    if task is not None:
                        output.append(f"<!-- T{task} -->\n")
                        output.append("\n")
                        tags_inserted += 1
                    current_tag = task

            output.append(line)

        # Close final tag if still open
        if current_tag is not None:
            output.append(f"\n<!-- /T{current_tag} -->\n")
            tags_inserted += 1

        if dry_run:
            print(f"Would insert {tags_inserted} tags into chat_log.md")
            # Show first few transitions
            current_tag = None
            for line in output:
                stripped = line.strip()
                if existing_tag.match(stripped):
                    print(f"  {stripped}")
        else:
            updated_chat = "".join(output).encode("utf-8")
            try:
                with chat_log_lock(chat_log.parent):
                    _prepare_merge_require_snapshot(chat_log, chat_before, "chat-log")
                    _prepare_merge_atomic_write(chat_log, updated_chat)
            except (OSError, TaskDocumentError) as exc:
                print(f"Error: chat tagging refused: {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"Inserted {tags_inserted} tags into chat_log.md")

    elif cmd == "retro":
        project_path = find_project_root()
        # Parse --since N flag
        since = 0
        i = 0
        while i < len(cmd_args):
            if cmd_args[i] == "--since" and i + 1 < len(cmd_args):
                try:
                    since = int(cmd_args[i + 1])
                except ValueError:
                    print(f"Error: --since requires a number", file=sys.stderr)
                    sys.exit(1)
                i += 2
            else:
                i += 1

        from tasks.retro import (
            extract_tasks, extract_chatlog, extract_mindmap,
            build_task_windows,
        )

        tasks_dir = resolve_agent_dir(project_path) / "tasks"
        chatlog_path = resolve_agent_dir(project_path) / "chat_log.md"
        bash_history_path = resolve_agent_dir(project_path) / "bash_history"
        mindmap_path = project_path / "MIND_MAP.md"

        # Extract data
        tasks = extract_tasks(tasks_dir, since=since)
        task_windows = build_task_windows(chatlog_path, bash_history_path)
        chatlog = extract_chatlog(chatlog_path, task_windows)
        mindmap = extract_mindmap(mindmap_path)

        if not tasks:
            print("No tasks found in window.", file=sys.stderr)
            sys.exit(1)

        # Run structural analysis passes
        from tasks.retro import (
            analyze_intent_health, analyze_garbage,
            generate_retro_task,
        )
        health = analyze_intent_health(tasks)
        gc = analyze_garbage(tasks)

        # Generate the retro task.md — a cognitive program
        retro_content = generate_retro_task(
            tasks=tasks, chatlog=chatlog, mindmap=mindmap,
            health=health, gc=gc,
        )

        # Create as a new task
        from tasks.core import _next_task_number, _slugify
        tasks_dir_path = resolve_agent_dir(project_path) / "tasks"
        task_num = _next_task_number(tasks_dir_path)
        first = tasks[0]["number"]
        last = tasks[-1]["number"]
        slug = f"retro-{first:03d}-{last:03d}"
        folder_name = f"{task_num:03d}-{slug}"
        task_dir = tasks_dir_path / folder_name
        task_dir.mkdir(parents=True)
        task_file = task_dir / "task.md"
        task_file.write_text(retro_content, encoding="utf-8")

        print(f"Created: {task_file.relative_to(project_path)}")
        print(f"Retro task T{task_num:03d} — {len(tasks)} tasks in window, "
              f"{len(chatlog)} chat messages, {len(mindmap)} mind map nodes")
        print(f"Next: pb-tasks work {task_num}")

    elif cmd == "global-retro-collect":
        since = None
        machine = None
        out_dir = Path.cwd()
        archive_format = "zip"
        roots = []
        i = 0
        while i < len(cmd_args):
            arg = cmd_args[i]
            if arg == "--since" and i + 1 < len(cmd_args):
                since = cmd_args[i + 1]
                i += 2
            elif arg == "--machine" and i + 1 < len(cmd_args):
                machine = cmd_args[i + 1]
                i += 2
            elif arg == "--out" and i + 1 < len(cmd_args):
                out_dir = Path(cmd_args[i + 1])
                i += 2
            elif arg == "--format" and i + 1 < len(cmd_args):
                archive_format = cmd_args[i + 1]
                i += 2
            elif arg.startswith("--"):
                print(f"Error: unknown option for global-retro-collect: {arg}", file=sys.stderr)
                print("Usage: pb-tasks global-retro-collect --since DATE [--machine NAME] [--out DIR] [--format zip|tgz] ROOT [ROOT...]", file=sys.stderr)
                sys.exit(1)
            else:
                roots.append(Path(arg))
                i += 1

        if since is None:
            print("Error: global-retro-collect requires --since DATE", file=sys.stderr)
            print("Usage: pb-tasks global-retro-collect --since DATE [--machine NAME] [--out DIR] [--format zip|tgz] ROOT [ROOT...]", file=sys.stderr)
            sys.exit(1)
        if not roots:
            print("Error: global-retro-collect requires at least one root directory", file=sys.stderr)
            print("Usage: pb-tasks global-retro-collect --since DATE [--machine NAME] [--out DIR] [--format zip|tgz] ROOT [ROOT...]", file=sys.stderr)
            sys.exit(1)

        try:
            from tasks.global_retro_collect import collect_global_retro
            archive_path, manifest = collect_global_retro(
                roots=roots,
                since=since,
                out_dir=out_dir,
                machine=machine,
                archive_format=archive_format,
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

        kept = sum(1 for project in manifest["projects"] if project["kept"])
        task_count = sum(len(project["included_tasks"]) for project in manifest["projects"])
        file_count = sum(len(project["included_files"]) for project in manifest["projects"])
        print(f"Created: {archive_path}")
        print(
            f"Global retro collection: {kept} project(s), "
            f"{task_count} task(s), {file_count} file(s)"
        )
        print("Includes manifest.json and manifest.tsv")

    elif cmd == "status":
        project_path = find_project_root()
        task_status(project_path)

    elif cmd == "freehand":
        project_path = find_project_root()
        sub = cmd_args[0] if cmd_args else None

        if sub == "log":
            # Extract chat_log messages from freehand-start to now into task.md
            agent_dir = resolve_agent_dir(project_path)
            state_file = _state_file(project_path)
            if not state_file.exists():
                print("Error: no active task", file=sys.stderr)
                sys.exit(1)
            task_num = state_file.read_text(encoding="utf-8").strip()
            key = resolve_session_key()
            try:
                task_file = validate_task_claim(agent_dir, key, task_num)
                task_text = task_file.read_text(encoding="utf-8")
                task_document = TaskDocument.parse(task_text)
            except (OSError, TaskDocumentError) as exc:
                print(f"Error: cannot log freehand task {task_num}: {exc}", file=sys.stderr)
                sys.exit(1)

            # Find the freehand-start marker
            import re
            # Use findall + take last — supports multiple freehand blocks in one task
            all_markers = re.findall(r'<!-- freehand-start: (.+?) -->', task_text)
            marker_match = all_markers[-1] if all_markers else None
            if not marker_match:
                print("Error: no freehand-start marker found in task.md", file=sys.stderr)
                sys.exit(1)

            # Parse the start timestamp
            from datetime import datetime, timezone
            start_str = marker_match.strip()
            try:
                start_ts = datetime.fromisoformat(start_str)
                if start_ts.tzinfo is None:
                    start_ts = start_ts.replace(tzinfo=timezone.utc)
            except ValueError:
                print(f"Error: cannot parse freehand-start timestamp: {start_str}", file=sys.stderr)
                sys.exit(1)

            # Read chat_log.md and extract messages in the span
            chat_log = agent_dir / "chat_log.md"
            if not chat_log.exists():
                print("Error: .agent/chat_log.md not found", file=sys.stderr)
                sys.exit(1)

            from tasks.chat_state import parse_chat_entries
            extracted = []
            for entry in parse_chat_entries(
                chat_log.read_text(encoding="utf-8")
            ):
                if not entry.marker.startswith("M"):
                    continue
                try:
                    msg_ts = datetime.strptime(
                        entry.timestamp.removesuffix(" UTC"),
                        "%Y-%m-%d %H:%M:%S",
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if msg_ts >= start_ts:
                    extracted.append(entry.raw)

            if not extracted:
                print("No chat_log messages found in freehand span.")
                return

            # Insert exact message bodies below the last semantic Freehand log
            # gate. Opaque markers prevent captured headings/checkboxes from
            # becoming task authority or executable gates.
            log_gates = [
                gate for gate in task_document.gates
                if not gate.checked and gate.text.startswith("Freehand log")
            ]
            if not log_gates:
                print("Error: no '- [ ] Freehand log' gate found in task.md", file=sys.stderr)
                sys.exit(1)
            gate = log_gates[-1]
            insert_pos = sum(len(line) for line in task_document.lines[:gate.line + 1])
            log_content = (
                "\n<!-- playbook-recent-chat:start -->\n"
                + "\n\n---\n\n".join(extracted)
                + "\n<!-- playbook-recent-chat:end -->\n"
            )
            new_text = task_text[:insert_pos] + log_content + task_text[insert_pos:]
            try:
                replace_claimed_task_text(task_file, key, task_text, new_text)
            except (OSError, TaskDocumentError) as exc:
                print(f"Error: freehand log changed before commit: {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"Inserted {len(extracted)} chat_log messages into task.md")
            return

        # Main freehand command: insert Freehand block into active task
        state_file = _state_file(project_path)
        agent_dir = resolve_agent_dir(project_path)

        if state_file.exists():
            task_num = state_file.read_text(encoding="utf-8").strip()
        else:
            task_num = None

        if not task_num:
            # Create through the normal unclaimed task path, then publish the
            # task.md claim before its rebuildable session navigation cache.
            print("No active task — creating freehand session...")
            task_file = create_task(
                project_path, "freehand", task_type="quick", stub=True
            )
            task_num = task_file.parent.name.partition("-")[0].zfill(3)
            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            freehand_text = (
                f"# {task_num} - Freehand\n\n"
                f"## Status\npending\n\n"
                f"## Intent\n(freehand session — intent determined during work)\n\n"
                f"## Work Plan\n\n"
                f"### Freehand\n"
                f"<!-- freehand-start: {now_iso} -->\n"
                f"- [ ] Freehand\n"
                f"- [ ] Freehand log — run `pb-tasks freehand log` to capture chat_log messages, "
                f"then retro-add checked gates for work done\n"
                f"- [ ] Rewrite this freehand work into normal task gates inside this task so the final trace reads like ordinary tracked work\n"
                f"- [ ] Rename this task folder and header to match what was actually done, then check this gate last\n"
            )
            original = task_file.read_text(encoding="utf-8")
            key = resolve_session_key()
            replace_unclaimed_task_text(task_file, original, freehand_text)
            claim_task_document(task_file, key)
            try:
                write_navigation_cache(agent_dir, key, task_num)
            except (OSError, SessionStateError) as exc:
                print(
                    f"Error: freehand task {task_num} is claimed but cache publication "
                    f"failed; run pb-tasks work {task_num}: {exc}",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(f"Created and activated task {task_num}")
        else:
            # Work mode: insert freehand block into current task
            key = resolve_session_key()
            try:
                task_file = validate_task_claim(agent_dir, key, task_num)
                task_text = task_file.read_text(encoding="utf-8")
                task_document = TaskDocument.parse(task_text)
            except (OSError, TaskDocumentError) as exc:
                print(f"Error: cannot enter freehand for task {task_num}: {exc}", file=sys.stderr)
                sys.exit(1)

            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            freehand_block = (
                f"\n### Freehand\n"
                f"<!-- freehand-start: {now_iso} -->\n"
                f"- [ ] Freehand\n"
                f"- [ ] Freehand log — run `pb-tasks freehand log` to capture chat_log messages, "
                f"then retro-add checked gates for work done\n"
                f"- [ ] Rewrite this freehand work into normal task gates inside this task so the final trace reads like ordinary tracked work\n"
                f"- [ ] Rename this task folder and header to match what was actually done, then check this gate last\n"
            )

            # Insert before the first semantic gate in Work Plan. Fenced and
            # captured-chat examples are never candidate instruction pointers.
            work_plan = task_document.section_span("Work Plan")
            if work_plan:
                _, section_end = work_plan
                gate_lines = [
                    gate.line for gate in task_document.gates
                    if work_plan[0] < gate.line < section_end
                ]
                insert_line = gate_lines[0] if gate_lines else section_end
                insert_pos = sum(len(line) for line in task_document.lines[:insert_line])
            else:
                insert_pos = len(task_text)

            new_text = task_text[:insert_pos] + freehand_block + "\n" + task_text[insert_pos:]
            try:
                replace_claimed_task_text(task_file, key, task_text, new_text)
            except (OSError, TaskDocumentError) as exc:
                print(f"Error: freehand task changed before commit: {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"Freehand block inserted in task {task_num}")
        print(f"Freehand mode active. Agent: wait for user instructions. Close only when user says done.")

    elif cmd == "doctor":
        project_path = find_project_root()
        passed = 0
        failed = 0

        def iter_hook_commands(node):
            if isinstance(node, dict):
                command = node.get("command")
                if isinstance(command, str):
                    yield command
                for value in node.values():
                    yield from iter_hook_commands(value)
            elif isinstance(node, list):
                for item in node:
                    yield from iter_hook_commands(item)

        def check(name: str, ok: bool, detail: str = ""):
            nonlocal passed, failed
            status = "PASS" if ok else "FAIL"
            msg = f"  [{status}] {name}"
            if detail:
                msg += f" — {detail}"
            print(msg)
            if ok:
                passed += 1
            else:
                failed += 1

        print("pb-tasks doctor\n")

        # 1. Project structure
        agent_tasks = resolve_agent_dir(project_path) / "tasks"
        check("project: tasks/ exists", agent_tasks.exists())
        guidance_files = tuple(
            name for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md")
            if (project_path / name).is_file()
        )
        check(
            "project: provider guidance exists",
            bool(guidance_files),
            ", ".join(guidance_files) if guidance_files else "none found",
        )
        mind_map = project_path / "MIND_MAP.md"
        check("project: MIND_MAP.md exists", mind_map.exists())

        # 2. Unicode
        stdout_enc = getattr(sys.stdout, "encoding", "unknown") or "unknown"
        check("unicode: stdout encoding", "utf" in stdout_enc.lower(), stdout_enc)

        # 3. Native state is durable; validate native candidates without
        # treating historical PID/generated directories as live authority.
        agent_dir = resolve_agent_dir(project_path)
        sessions_dir = agent_dir / "sessions"
        unsafe_sessions = sessions_dir.is_symlink()
        recognized = ()
        inert = ()
        malformed = ()
        if not unsafe_sessions:
            try:
                recognized, inert, malformed = inspect_session_directories(agent_dir)
            except (OSError, SessionStateError):
                unsafe_sessions = True
        check("session: durable state is path-confined", not unsafe_sessions,
              "no symlinked session root/entries" if not unsafe_sessions else "unsafe session symlink")
        if not unsafe_sessions:
            malformed_detail = ", ".join(
                path.relative_to(agent_dir).as_posix() for path in malformed[:3]
            )
            check(
                "session: native records are valid",
                not malformed,
                malformed_detail if malformed else f"{len(recognized)} recognized",
            )
            check(
                "session: legacy/generated directories are inert",
                True,
                f"{len(recognized)} recognized provider-native; {len(inert)} inert ignored",
            )

        # 4. Runtime closure belongs to the checkout serving this CLI, never
        # to the project being diagnosed.
        runtime_root = Path(__file__).resolve().parents[2]
        runtime_errors = audit_serving_runtime(runtime_root)
        check(
            "runtime: serving hook closure",
            not runtime_errors,
            "installed/development runtime is complete"
            if not runtime_errors
            else "; ".join(runtime_errors[:3])
            + "; repair: reinstall the Playbook Harness runtime",
        )

        # 4a. Init and doctor share one read-only legacy classifier.
        from tasks.legacy_migration import inspect_legacy_project

        legacy = inspect_legacy_project(
            project_path, include_hooks=True, home=Path.home()
        )
        if legacy.conflicts:
            migration_ok = False
            migration_detail = "active legacy/manual conflict: " + "; ".join(
                legacy.conflicts[:3]
            )
        elif legacy.migrated:
            migration_ok = False
            migration_detail = (
                "migratable legacy: " + ", ".join(legacy.migrated)
                + "; repair: run pb-tasks init"
            )
        else:
            migration_ok = True
            migration_detail = "current (no active legacy runtime)"
        check("project: standalone migration state", migration_ok, migration_detail)

        # 4b. Check ~/.claude/settings.json for stale hook entries pointing to nonexistent paths
        user_settings = Path.home() / ".claude" / "settings.json"
        stale_hooks = []
        if user_settings.exists():
            import json as _json
            try:
                settings = _json.loads(user_settings.read_text(encoding="utf-8"))
                for cmd in iter_hook_commands(settings.get("hooks", {})):
                    for token in cmd.split():
                        p = Path(token)
                        if p.suffix in (".sh", "") and len(p.parts) > 2 and not p.exists():
                            stale_hooks.append(str(p))
            except (ValueError, KeyError):
                pass
        check("hooks: no stale entries in ~/.claude/settings.json",
              len(stale_hooks) == 0,
              f"stale paths: {', '.join(stale_hooks[:3])}" if stale_hooks else "clean")

        # 4c. Standalone init intentionally does not install provider-global
        # hooks, but a previously installed Agy plugin still affects this
        # project. Make that effective capability and its provenance visible.
        agy_plugin = Path.home() / ".gemini" / "config" / "plugins" / "playbook-harness"
        if not agy_plugin.exists():
            check("hooks: Agy global plugin", True, "inactive (not installed)")
        else:
            hook_manifests = sorted(agy_plugin.rglob("hooks.json"))
            expected_manifest = agy_plugin / "hooks.json"
            bridge_paths = []
            manifest_error = ""
            try:
                hook_doc = json.loads(expected_manifest.read_text(encoding="utf-8"))
                for command in iter_hook_commands(hook_doc):
                    for token in command.split():
                        if token.endswith("agy-hook-bridge.py"):
                            bridge_paths.append(Path(token))
            except (OSError, ValueError) as exc:
                manifest_error = str(exc)

            agy_bin = shutil.which("agy")
            imported = False
            list_error = "agy is not on PATH"
            if agy_bin:
                listed = subprocess.run(
                    [agy_bin, "plugin", "list"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                try:
                    imports = json.loads(listed.stdout).get("imports", [])
                    imported = any(
                        item.get("name") == "playbook-harness"
                        and "hooks" in item.get("components", [])
                        for item in imports
                        if isinstance(item, dict)
                    )
                    if not imported:
                        list_error = "not present in `agy plugin list`"
                except (AttributeError, ValueError):
                    list_error = listed.stderr.strip() or "unparseable `agy plugin list` output"

            unique_bridges = sorted({str(path) for path in bridge_paths})
            plugin_ok = (
                hook_manifests == [expected_manifest]
                and not manifest_error
                and bool(unique_bridges)
                and all(Path(path).is_file() for path in unique_bridges)
                and imported
            )
            if plugin_ok:
                detail = "active global hooks; bridge=" + ", ".join(unique_bridges)
            else:
                problems = []
                if hook_manifests != [expected_manifest]:
                    relative = [path.relative_to(agy_plugin).as_posix() for path in hook_manifests]
                    problems.append("hook manifests=" + ", ".join(relative or ["none"]))
                if manifest_error:
                    problems.append("invalid root manifest: " + manifest_error)
                if not unique_bridges:
                    problems.append("no bridge provenance")
                elif not all(Path(path).is_file() for path in unique_bridges):
                    problems.append("missing bridge=" + ", ".join(unique_bridges))
                if not imported:
                    problems.append(list_error)
                detail = "; ".join(problems)
            check("hooks: Agy global plugin", plugin_ok, detail)

        # 5. Runtime identity (public artifact or development checkout)
        from tasks.core import VERSION as code_version
        artifact_manifest = runtime_root / ".playbook-artifact.json"
        identified = artifact_manifest.is_file() or (runtime_root / ".git").exists()
        runtime_kind = "public artifact" if artifact_manifest.is_file() else "development checkout"
        check("runtime: checkout identified", identified,
              f"{runtime_kind}, code={code_version}" if identified else str(runtime_root))
        try:
            generation_surface, generation_ok = runtime_generation_status(project_path)
        except RuntimeError as exc:
            generation_surface, generation_ok = str(exc), False
        check(
            "runtime: project generation compatible with serving CLI",
            generation_ok,
            generation_surface.replace("\n", "; "),
        )

        # 6. Python version
        import platform
        py_ver = platform.python_version()
        major, minor = sys.version_info[:2]
        check("python: version >= 3.8", major >= 3 and minor >= 8, py_ver)

        # 7. write_text encoding (check installed plugin scripts)
        import re as _re
        import inspect
        cli_src = Path(inspect.getfile(sys.modules[__name__]))
        core_src = cli_src.parent / "core.py"
        unencoded = 0
        for src_file in [cli_src, core_src]:
            if src_file.exists():
                content = src_file.read_text(encoding="utf-8")
                # Find all write_text/read_text calls (may span multiple lines)
                for m in _re.finditer(r'\.(write_text|read_text)\(', content):
                    # Find the matching closing paren
                    start = m.end()
                    depth = 1
                    pos = start
                    while pos < len(content) and depth > 0:
                        if content[pos] == '(':
                            depth += 1
                        elif content[pos] == ')':
                            depth -= 1
                        pos += 1
                    call_body = content[start:pos]
                    if "encoding=" not in call_body:
                        unencoded += 1
        check("encoding: write_text/read_text have encoding=", unencoded == 0,
              f"{unencoded} unencoded calls" if unencoded else "all encoded")

        # Summary
        total = passed + failed
        print(f"\n{passed}/{total} checks passed", end="")
        if failed:
            print(f" ({failed} failed)")
        else:
            print()

    elif cmd == "log":
        # tasks log [N] [--width W]
        # Compact one-line-per-message view of chat_log.md (no gate cruft).
        # N: show only the last N messages (default: all).
        # --width: crop each message body to W chars (default 500).
        cmd_args = sys.argv[2:]
        last_n = None
        width = 500
        i = 0
        while i < len(cmd_args):
            a = cmd_args[i]
            if a == "--width" and i + 1 < len(cmd_args):
                width = max(10, int(cmd_args[i + 1])); i += 2
            elif a.isdigit():
                last_n = int(a); i += 1
            else:
                i += 1
        project_path = find_project_root()
        chat_log = resolve_agent_dir(project_path) / "chat_log.md"
        if not chat_log.exists():
            print("Error: .agent/chat_log.md not found", file=sys.stderr)
            sys.exit(1)
        for line in _compact_chat_lines(chat_log, last_n=last_n, width=width):
            print(line)

    elif cmd == "narrative":
        from . import narrative as narrative_mod

        project_path = find_project_root()
        action = "status"
        lines_back: int | None = None
        limit: int | None = None
        remaining = list(cmd_args)
        while remaining:
            a = remaining.pop(0)
            if a in ("--render", "--pending", "--status"):
                action = a.lstrip("-")
            elif a == "--lines" and remaining:
                try:
                    lines_back = int(remaining.pop(0))
                except ValueError:
                    print("Error: --lines requires a number", file=sys.stderr)
                    sys.exit(1)
            elif a == "--limit" and remaining:
                try:
                    limit = int(remaining.pop(0))
                except ValueError:
                    print("Error: --limit requires a number", file=sys.stderr)
                    sys.exit(1)
            else:
                print(f"Unknown argument: {a}", file=sys.stderr)
                sys.exit(1)
        try:
            if action == "pending":
                print(narrative_mod.dump_pending(project_path, limit, lines_back))
            else:
                report = (
                    narrative_mod.build(project_path, lines_back)
                    if action == "render"
                    else narrative_mod.status(project_path, lines_back)
                )
                for line in report.lines():
                    print(line)
        except narrative_mod.NarrativeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

    elif cmd == "prepare-merge":
        project_path = find_project_root()
        target = "main"
        dry_run = False
        remaining = list(cmd_args)
        while remaining:
            a = remaining.pop(0)
            if a == "--target" and remaining:
                target = remaining.pop(0)
            elif a == "--dry-run":
                dry_run = True
            else:
                print(f"Unknown argument: {a}", file=sys.stderr)
                sys.exit(1)
        _cmd_prepare_merge(project_path, target, dry_run)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print_usage()
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except NativeSessionIdentityError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
