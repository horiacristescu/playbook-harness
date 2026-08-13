"""Retrospective analysis for task history.

Extracts structured data from task.md files, chat_log.md, and MIND_MAP.md
for project-level retrospective analysis.
"""
from __future__ import annotations

import re
from pathlib import Path

from tasks.chat_state import parse_chat_entries
from tasks.task_document import TaskDocument, TaskDocumentError


def extract_tasks(tasks_dir: Path, since: int = 0) -> list[dict]:
    """Extract structured data from task.md files.

    Args:
        tasks_dir: Path to .agent/tasks/ directory
        since: Only include tasks with number >= since (0 = all)

    Returns list of dicts with keys:
        number, title, intent, why, status, gate_count, checked_count,
        bare_checkmark_count, gate_texts, parked_items, playbook_type
    """
    if not tasks_dir.exists():
        return []

    results = []
    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        m = re.match(r'^(\d+)-(.+)$', task_dir.name)
        if not m:
            continue
        num = int(m.group(1))
        if num < since:
            continue

        task_file = task_dir / "task.md"
        if not task_file.exists():
            continue

        content = task_file.read_text(encoding="utf-8")
        results.append(_parse_task(num, m.group(2), content))

    return results


def _parse_task(num: int, slug: str, content: str) -> dict:
    """Parse a task.md file into structured data."""
    lines = content.splitlines()

    # Extract sections
    intent = _extract_section(lines, "Intent")
    why = _extract_section(lines, "Why")
    try:
        status = TaskDocument.parse(content).status
    except TaskDocumentError:
        status = "error"
    parked = _extract_section(lines, "Parked")

    # Gate analysis
    gate_pattern = re.compile(r'^\s*- \[( |x|X)\]\s*(.*)')
    gates = []
    for line in lines:
        m = gate_pattern.match(line)
        if m:
            checked = m.group(1) in ('x', 'X')
            text = m.group(2).strip()
            gates.append({"checked": checked, "text": text})

    checked_count = sum(1 for g in gates if g["checked"])
    # Bare checkmark: checked gate where the agent didn't append any outcome.
    # Heuristic: text is very short (≤60 chars) and doesn't contain outcome markers
    # like " — ", ":", ".", or result words. Template gates are typically short labels.
    bare_count = 0
    for g in gates:
        if not g["checked"]:
            continue
        text = g["text"]
        # Long text = agent wrote something substantive
        if len(text) > 60:
            continue
        # Contains outcome markers = annotated
        if any(marker in text for marker in [" — ", " - ", "✓", "✗", "passing", "passed", "fixed"]):
            continue
        if re.search(r':\s+\S|\.\s+\S', text):
            continue
        bare_count += 1

    # Parked items
    parked_items = []
    if parked and parked.strip() not in ("", "(Findings or ideas that emerged during work but are out of scope. Describe each with enough context for a future task to pick it up.)"):
        for line in parked.splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                parked_items.append(stripped[2:])

    # Detect playbook type from content
    playbook_type = _detect_type(content)

    return {
        "number": num,
        "title": slug.replace("-", " ").title(),
        "intent": intent,
        "why": why,
        "status": status,
        "gate_count": len(gates),
        "checked_count": checked_count,
        "bare_checkmark_count": bare_count,
        "gate_texts": [g["text"] for g in gates],
        "parked_items": parked_items,
        "playbook_type": playbook_type,
    }


def _extract_section(lines: list[str], heading: str) -> str:
    """Extract content between ## heading and next ## heading."""
    in_section = False
    section_lines = []
    for line in lines:
        if line.strip() == f"## {heading}":
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            section_lines.append(line)
    return "\n".join(section_lines).strip()


def _detect_type(content: str) -> str:
    """Detect task type from content heuristics."""
    if "<!-- stub:" in content:
        m = re.search(r'<!-- stub:(\w+) -->', content)
        return f"stub:{m.group(1)}" if m else "stub"
    if "## Design Phase" not in content and "## Work" in content:
        return "quick"
    if "### Round" in content:
        return "investigate"
    if "### Lenses" in content or "### Verdict" in content:
        return "evaluate"
    if "## Design Phase" in content:
        return "build"
    return "unknown"


def extract_chatlog(path: Path, task_windows: dict[int, tuple[str, str]] | None = None) -> list[dict]:
    """Extract messages from chat_log.md.

    Args:
        path: Path to chat_log.md
        task_windows: Optional dict mapping task number → (start_timestamp, end_timestamp).
            If provided, each message gets a 'task' field with the task number it falls within.

    Returns list of dicts with keys: id, timestamp, speaker, text, task (optional)
    """
    if not path.exists():
        return []

    messages = []
    for entry in parse_chat_entries(path.read_text(encoding="utf-8")):
        if not entry.marker.startswith("M"):
            continue
        msg = {
            "id": int(entry.marker[1:]),
            "timestamp": entry.timestamp,
            "speaker": entry.speaker,
            "text": entry.body,
            "provider": entry.provider,
            "session_id": entry.session_id,
            "session_name": entry.session_name,
        }

        # Attribute to task window if available
        if task_windows:
            msg["task"] = _attribute_to_task(entry.timestamp, task_windows)

        messages.append(msg)

    return messages


def _normalize_ts(ts: str) -> str:
    """Strip ' UTC' suffix for consistent comparison."""
    return ts.replace(" UTC", "").strip()


def _attribute_to_task(timestamp: str, task_windows: dict[int, tuple[str, str]]) -> int | None:
    """Find which task window a timestamp falls into."""
    ts = _normalize_ts(timestamp)
    for task_num, (start, end) in task_windows.items():
        if _normalize_ts(start) <= ts < _normalize_ts(end):  # F2: exclusive end
            return task_num
    return None


def build_task_windows(chatlog_path: Path, bash_history_path: Path | None = None) -> dict[int, tuple[str, str]]:
    """Build task number → (start_timestamp, end_timestamp) mapping.

    Scans chat_log.md gate entries and bash_history for 'tasks work <N>' activations.
    Each task's window extends from its activation to the next task's activation.
    """
    windows: dict[int, str] = {}  # task_num → activation timestamp

    # Typed task claims are the primary activation chronology. Historical gate
    # rows remain a fallback for logs written before T events existed.
    if chatlog_path.exists():
        for entry in parse_chat_entries(
            chatlog_path.read_text(encoding="utf-8")
        ):
            task_match = re.fullmatch(
                r"T(\d+):(claim|reopen|handoff|recover)", entry.marker
            )
            gate_match = re.fullmatch(r"G(\d+):\d+", entry.marker)
            match = task_match or gate_match
            if match is None:
                continue
            task_num = int(match.group(1))
            ts = entry.timestamp
            if task_num not in windows or ts < windows[task_num]:
                windows[task_num] = ts

    # Scan bash_history for 'tasks work <N>' entries
    if bash_history_path and bash_history_path.exists():
        content = bash_history_path.read_text(encoding="utf-8")
        work_pattern = re.compile(
            r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+\|\s+\w+\s+\|\s+.*tasks\s+work\s+(\d+)'
        )
        for m in work_pattern.finditer(content):
            ts = m.group(1).strip()
            task_num = int(m.group(2))
            if task_num not in windows or ts < windows[task_num]:
                windows[task_num] = ts

    if not windows:
        return {}

    # Convert to (start, end) ranges: each task ends when the next one starts
    sorted_tasks = sorted(windows.items(), key=lambda x: x[1])
    result = {}
    for i, (task_num, start_ts) in enumerate(sorted_tasks):
        if i + 1 < len(sorted_tasks):
            end_ts = sorted_tasks[i + 1][1]
        else:
            end_ts = "9999-12-31 23:59:59 UTC"  # still active
        result[task_num] = (start_ts, end_ts)

    return result


def extract_mindmap(path: Path) -> list[dict]:
    """Extract nodes from MIND_MAP.md.

    Returns list of dicts with keys: id, text, size (bytes)
    """
    if not path.exists():
        return []

    content = path.read_text(encoding="utf-8")
    nodes = []

    # Pattern: [N] **Title** — description
    node_pattern = re.compile(r'^\[(\d+)\]\s+(.*)', re.MULTILINE)

    for m in node_pattern.finditer(content):
        node_id = int(m.group(1))
        text = m.group(2).strip()
        nodes.append({
            "id": node_id,
            "text": text,
            "size": len(text.encode("utf-8")),
        })

    return nodes


# --- Retro task generator ---

def generate_retro_task(
    tasks: list[dict],
    chatlog: list[dict],
    mindmap: list[dict],
    health: list[dict],
    gc: dict,
) -> str:
    """Generate a retro task.md — a cognitive program for the agent to execute.

    The structural scan provides data. The gates are the analysis.
    The agent works through them with full playbook enforcement.
    """
    first = tasks[0]["number"]
    last = tasks[-1]["number"]
    total_gates = sum(t["gate_count"] for t in tasks)
    total_checked = sum(t["checked_count"] for t in tasks)
    total_bare = sum(t["bare_checkmark_count"] for t in tasks)
    total_parked = sum(len(t["parked_items"]) for t in tasks)
    total_mm_size = sum(n["size"] for n in mindmap)
    msg_count = len(chatlog)

    lines = []

    # Header
    lines.append(f"# Retro {first:03d}–{last:03d}")
    lines.append("")
    lines.append("> **Intent is satisfied top-down:** user words → Intent → gates → code → tests.")
    lines.append("> Does each level faithfully serve the one above?")
    lines.append("> **Work is justified bottom-up:** tests → code → gates → Intent → user words.")
    lines.append("> Does each level have evidence backing it from below?")
    lines.append("")

    # Status
    lines.append("## Status")
    lines.append("pending")
    lines.append("")

    # Structural summary
    lines.append("## Structural Summary")
    lines.append("")
    lines.append(f"**Window:** {len(tasks)} tasks (T{first:03d}–T{last:03d}), "
                 f"{msg_count} chat messages, {len(mindmap)} mind map nodes")
    lines.append(f"**Gates:** {total_checked}/{total_gates} checked, "
                 f"{total_bare} bare ({total_bare*100//max(total_checked,1)}%), "
                 f"{total_parked} parked items")
    lines.append(f"**Mind map:** {total_mm_size:,} bytes ({len(mindmap)} nodes, load budget 25,000)")
    lines.append("")

    # Task inventory table
    lines.append("| # | Title | Status | Gates | Bare | Type |")
    lines.append("|---|-------|--------|-------|------|------|")
    for t in tasks:
        title_short = t["title"][:30]
        lines.append(
            f"| {t['number']:03d} | {title_short} | {t['status'][:7]} | "
            f"{t['checked_count']}/{t['gate_count']} | {t['bare_checkmark_count']} | "
            f"{t['playbook_type']} |"
        )
    lines.append("")

    # Pending / loose ends summary
    if gc["pending"] or gc["loose_ends"]:
        lines.append("**Loose ends:**")
        for p in gc["pending"]:
            work = "has progress" if p["has_work"] else "not started"
            lines.append(f"- T{p['number']:03d} ({p['title']}): pending, {p['gates']} ({work})")
        for le in gc["loose_ends"]:
            if "unchecked" in le:
                lines.append(f"- T{le['number']:03d} ({le['title']}): done but {le['unchecked']} unchecked")
            elif "issue" in le:
                lines.append(f"- T{le['number']:03d} ({le['title']}): {le['issue']}")
        lines.append("")

    lines.append("---")
    lines.append("")

    # Phase 1: Chat log analysis
    lines.append("## Phase 1: Chat Log Analysis")
    lines.append("")
    lines.append("> Read the user's messages in the window. What were they trying to do?")
    lines.append("> Where did they struggle? What got corrected? What patterns emerge?")
    lines.append("")
    lines.append("- [ ] Read chat_log.md messages for this window (use `pb-tasks log` for a compact one-line-per-message view, or filter by message IDs listed above). Summarize: what was the user's arc? What themes dominated? What frustrations surfaced?")
    lines.append("- [ ] Identify steering moments — where did the user correct the agent? What was the nature of each correction (misframed intent / over-engineering / wrong abstraction / missing context / process friction)?")
    lines.append("- [ ] What inefficiencies show up? How many messages did simple things take? Where did the conversation loop?")
    lines.append("")

    # Phase 2: Per-task review
    lines.append("## Phase 2: Per-Task Review")
    lines.append("")
    lines.append("> For each task: Is intent **satisfied** (top-down)? Is work **justified** (bottom-up)?")
    lines.append("> Open the task.md, read Intent, scan gates. Then check the code and tests:")
    lines.append("> - Does the code exist? Do the files the gates reference actually contain what was claimed?")
    lines.append("> - Do tests exist? Do they test what the Intent says, not just what was easy to test?")
    lines.append("> - Trace all the way: user words → Intent → gates → code → tests (satisfied?) and back up (justified?)")
    lines.append("")
    for t in tasks:
        intent_short = t["intent"][:100].replace("\n", " ") if t["intent"] else "(no intent)"
        status = t["status"]
        bare = t["bare_checkmark_count"]
        flags = []
        if bare > 0:
            flags.append(f"{bare} bare")
        if status == "pending" and t["checked_count"] == t["gate_count"] and t["gate_count"] > 0:
            flags.append("done but not closed")
        if t["playbook_type"].startswith("stub"):
            flags.append("stub")
        if len(t["parked_items"]) > 0:
            flags.append(f"{len(t['parked_items'])} parked")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"- [ ] **T{t['number']:03d}** {t['title']}{flag_str} — {intent_short}")
    lines.append("")

    # Phase 3: Cross-task chains
    lines.append("## Phase 3: Cross-Task Chains")
    lines.append("")
    lines.append("> Pick facets that span multiple tasks. Follow each across the window.")
    lines.append("> Did intent carry forward? Did investments pay off? What got abandoned?")
    lines.append("")
    lines.append("- [ ] Identify 3-5 facets (systems, themes, or components) that appear across multiple tasks in this window.")
    lines.append("- [ ] For each facet: trace the chain. Did each task build on the previous? Did the work converge or scatter?")
    lines.append("- [ ] Which investments paid off? Which were abandoned? Any patterns in what succeeds vs what stalls?")
    lines.append("")

    # Phase 4: Garbage collection
    lines.append("## Phase 4: Garbage Collection")
    lines.append("")
    if gc["parked"]:
        lines.append("### Parked items to triage:")
        for p in gc["parked"]:
            marker = "pursued" if p["status"] == "pursued" else "waiting"
            lines.append(f"- [ ] T{p['task']:03d} [{marker}]: {p['text'][:120]}")
        lines.append("")
    lines.append("- [ ] Pending tasks: for each, decide — pursue, abandon, or merge into another task.")
    lines.append("- [ ] Loose ends: done-but-unchecked, stubs never expanded — close or reopen?")
    lines.append("")

    # Phase 5: Mind map revision
    lines.append("## Phase 5: Mind Map Revision")
    lines.append("")
    lines.append(f"> Current: {total_mm_size:,} bytes, {len(mindmap)} nodes. Load budget: 25,000 bytes (_load_mind_map).")
    lines.append("> Keep only what reduces future cost — reasoning, decisions, relationships.")
    lines.append("> Remove what's cheaply derivable from git log, grep, or reading code.")
    lines.append("")
    lines.append("- [ ] For each mind map node: does it carry information that would cost an agent significant time to rediscover? If not, compress or remove.")
    lines.append("- [ ] Are there new decisions, patterns, or relationships from this window that should be added?")
    lines.append("- [ ] Apply edits. Keep MIND_MAP.md ≤25KB (the load budget); route deep evidence to tasks, documentation, or source rather than a parallel map.")
    lines.append("")

    # Phase 6: Findings
    lines.append("## Findings")
    lines.append("")
    lines.append("- [ ] Synthesize: what worked well in this window? What needs to change?")
    lines.append("- [ ] Concrete proposals: tasks to file, process changes, template improvements.")
    lines.append("- [ ] What should the next retro investigate?")
    lines.append("")

    return "\n".join(lines) + "\n"


# --- Analysis passes ---

# Default gate counts per template type (approximate)
_TEMPLATE_DEFAULTS = {
    "build": 20,
    "quick": 3,
    "investigate": 15,
    "evaluate": 15,
    "unknown": 15,
}


def analyze_intent_health(tasks: list[dict]) -> list[dict]:
    """Pass 1: Score each task's intent health using structural signals.

    Returns list of dicts with keys:
        number, title, intent_present, bare_ratio, gate_adaptation, parked_count,
        hollowness (0.0-1.0, higher = worse)
    """
    results = []
    for t in tasks:
        checked = t["checked_count"]
        bare = t["bare_checkmark_count"]
        bare_ratio = bare / max(checked, 1)

        # Intent present = non-empty and not a placeholder
        intent_present = bool(t["intent"]) and not t["intent"].startswith("(")

        # Gate adaptation: how much did the task deviate from template default?
        default = _TEMPLATE_DEFAULTS.get(t["playbook_type"], 15)
        gate_adaptation = t["gate_count"] - default  # positive = added, negative = removed

        # Hollowness score: weighted combination
        # - Missing intent: +0.4
        # - High bare ratio: +0.4 * bare_ratio
        # - No adaptation (exactly template): +0.1
        # - No parked items on a large task: +0.1
        hollowness = 0.0
        if not intent_present:
            hollowness += 0.4
        hollowness += 0.4 * bare_ratio
        if gate_adaptation == 0 and t["gate_count"] > 5:
            hollowness += 0.1
        if len(t["parked_items"]) == 0 and t["gate_count"] > 15:
            hollowness += 0.1

        results.append({
            "number": t["number"],
            "title": t["title"],
            "intent_present": intent_present,
            "bare_ratio": bare_ratio,
            "gate_adaptation": gate_adaptation,
            "parked_count": len(t["parked_items"]),
            "hollowness": round(hollowness, 2),
        })

    # Sort by hollowness descending
    results.sort(key=lambda x: x["hollowness"], reverse=True)
    return results


def analyze_steering(chatlog: list[dict]) -> list[dict]:
    """Pass 2: Extract user corrections from chat log.

    Filters for short messages (≤100 words) containing correction markers.
    Returns list of dicts with keys: id, timestamp, task, text
    """
    correction_markers = [
        "no,", "no ", "not that", "instead ", "don't ", "stop ",
        "wrong", "i meant", "not what i", "that's not",
    ]
    # "let's" and "actually" and "wait" are too broad — normal instructions

    # Patterns to exclude (not corrections)
    exclude_patterns = [
        "You are a senior engineer",  # judge prompts
        "<task-notification>",
        "<ide_selection>",
        "tool-use-id",
    ]

    corrections = []
    for msg in chatlog:
        text = msg["text"]
        words = text.split()
        # Filter: short messages only
        if len(words) > 100:
            continue
        # Filter: exclude known non-correction patterns
        if any(pat in text for pat in exclude_patterns):
            continue
        # Filter: must contain a correction marker
        lower = text.lower()
        if not any(marker in lower for marker in correction_markers):
            continue
        corrections.append({
            "id": msg["id"],
            "timestamp": msg["timestamp"],
            "task": msg.get("task"),
            "text": text[:200],  # truncate for report
        })

    return corrections


def analyze_garbage(tasks: list[dict]) -> dict:
    """Pass 3: Garbage collection — parked items, pending tasks, loose ends.

    Returns dict with keys: parked (list), pending (list), loose_ends (list)
    """
    all_intents = " ".join(t["intent"] for t in tasks if t["intent"])

    # Parked items: check if pursued by later tasks
    parked = []
    for t in tasks:
        for item in t["parked_items"]:
            # Extract keywords from parked item (first 5 significant words)
            words = [w.lower().strip("*:,.-()") for w in item.split()[:10]
                     if len(w) > 4]  # F4: longer words to reduce false positives
            matches = sum(1 for w in words[:5] if w in all_intents.lower())
            pursued = matches >= 2  # F4: require 2+ keyword matches
            parked.append({
                "task": t["number"],
                "text": item[:150],
                "status": "pursued" if pursued else "still-waiting",
            })

    # Pending tasks
    pending = []
    for t in tasks:
        if t["status"] == "pending":
            pending.append({
                "number": t["number"],
                "title": t["title"],
                "gates": f"{t['checked_count']}/{t['gate_count']}",
                "has_work": t["checked_count"] > 0,
            })

    # Loose ends: done tasks with unchecked gates, stubs
    loose_ends = []
    for t in tasks:
        if t["status"] == "done" and t["checked_count"] < t["gate_count"]:
            loose_ends.append({
                "number": t["number"],
                "title": t["title"],
                "unchecked": t["gate_count"] - t["checked_count"],
            })
        if t["playbook_type"].startswith("stub"):
            loose_ends.append({
                "number": t["number"],
                "title": t["title"],
                "issue": "stub not expanded",
            })

    return {"parked": parked, "pending": pending, "loose_ends": loose_ends}


def analyze_mindmap(nodes: list[dict]) -> list[dict]:
    """Pass 4: Mind map reconciliation.

    Flag nodes that are derivable from repo, too large, or likely stale.
    Returns list of dicts with keys: id, text_preview, size, proposal, reason
    """
    # Patterns that indicate derivable/compressible content
    derivable_patterns = [
        (re.compile(r'`[/\w]+\.\w+`'), "contains file paths (derivable from repo)"),
        (re.compile(r'\d+ tests?'), "contains test counts (derivable from test suite)"),
    ]

    proposals = []
    for n in nodes:
        text = n["text"]
        size = n["size"]

        # Large nodes
        if size > 500:
            proposals.append({
                "id": n["id"],
                "text_preview": text[:80],
                "size": size,
                "proposal": "compress",
                "reason": f"Large node ({size} bytes) — review for compressible content",
            })
            continue  # F3: don't double-flag

        # Check for derivable patterns (only for small nodes)
        for pattern, reason in derivable_patterns:
            if pattern.search(text):
                proposals.append({
                    "id": n["id"],
                    "text_preview": text[:80],
                    "size": size,
                    "proposal": "review",
                    "reason": reason,
                })
                break  # one flag per node

    return proposals
