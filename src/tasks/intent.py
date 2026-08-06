"""Intent review — the "vertical retro".

Infers intent independently from the four layers of a single work unit
(chat_log -> task.md -> code/diff -> tests), blind, via the judge sandbox; the
main agent + user then reconcile the four reports in-session. See task 141.

This is the depth-wise sibling of `tasks retro` (horizontal, across many tasks).
Vertical drills down ONE unit's stack and checks coherence top-to-bottom; the
three seams localize where drift entered:
    chat -> task.md   = comprehension
    task.md -> code   = execution
    code -> tests     = verification

Stdlib-only (runtime invariant): shells out to `git` for the code/test layers.
The model only does the 4 blind extractions; reconciliation/grading is human.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Per-layer evidence budget. Large chat spans / diffs get head+tail trimmed so
# the default judge model isn't blown past its context (same idea as
# cli._load_mind_map). 20K chars ~= 5K tokens/layer.
LAYER_BUDGET = 20_000

LAYERS = ("chat", "taskmd", "code", "tests")

# git's canonical empty-tree hash — used as the diff base when the earliest
# task commit is the repo root (has no parent), so the diff is the full commit.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


@dataclass
class Slice:
    """One layer's evidence, with provenance so the judge never fabricates.

    available=False means "no evidence for this layer" — the extraction still
    runs on the other layers, and the report records the gap honestly rather
    than inventing intent.
    """

    layer: str
    text: str
    available: bool
    provenance: str


def _trim(text: str, budget: int = LAYER_BUDGET) -> str:
    """Head+tail trim to fit the per-layer budget, marking the elision."""
    if len(text) <= budget:
        return text
    half = budget // 2
    marker = "\n\n[... middle elided to fit layer budget ...]\n\n"
    return text[:half] + marker + text[-(budget - half - len(marker)):]


def find_task_dir(tasks_dir: Path, task_num: str) -> Path | None:
    """Resolve `.agent/tasks/NNN-*` for a zero-padded task number."""
    if not tasks_dir.exists():
        return None
    for child in sorted(tasks_dir.iterdir()):
        if child.is_dir() and child.name.startswith(f"{task_num}-"):
            return child
    return None


# ── Layer collectors ─────────────────────────────────────────────────────────

def collect_chat(agent_dir: Path, task_num: str, chat_file: Path | None = None) -> Slice:
    """Chat layer: tagged spans → override file → unavailable (never crash).

    Primary source is the `<!-- TNNN -->` … `<!-- /TNNN -->` spans in
    chat_log.md (same tags `tasks context` reads). An explicit --chat-file
    overrides. If nothing attributable is found, returns available=False with a
    clear note instead of fabricating.
    """
    if chat_file is not None:
        if not chat_file.exists():
            return Slice("chat", "", False, f"--chat-file not found: {chat_file}")
        return Slice("chat", _trim(chat_file.read_text(encoding="utf-8")),
                     True, f"--chat-file override: {chat_file}")

    chat_log = agent_dir / "chat_log.md"
    if not chat_log.exists():
        return Slice("chat", "", False, "no .agent/chat_log.md")

    open_tag = re.compile(r"^<!--\s*T" + re.escape(task_num) + r"\s*-->$")
    close_tag = re.compile(r"^<!--\s*/T" + re.escape(task_num) + r"\s*-->$")
    spans: list[str] = []
    current: list[str] = []
    inside = False
    for line in chat_log.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not inside and open_tag.match(stripped):
            inside = True
            continue
        if inside and close_tag.match(stripped):
            spans.append("\n".join(current))
            current = []
            inside = False
            continue
        if inside:
            current.append(line)
    if inside and current:
        spans.append("\n".join(current))

    if spans:
        return Slice("chat", _trim("\n\n".join(spans)), True,
                     f"{len(spans)} tagged span(s) in chat_log.md")

    # Fallback: timestamp-window attribution (untagged tasks). Build windows from
    # gate entries + bash_history `tasks work <N>`, then pull this task's messages.
    from tasks.retro import build_task_windows, extract_chatlog
    bash_history = agent_dir / "bash_history"
    windows = build_task_windows(chat_log, bash_history if bash_history.exists() else None)
    try:
        n = int(task_num)
    except ValueError:
        n = None
    if n in windows:
        msgs = [m for m in extract_chatlog(chat_log, windows) if m.get("task") == n]
        if msgs:
            text = "\n\n".join(f"[M{m['id']}] ({m['speaker']}) {m['text']}" for m in msgs)
            return Slice("chat", _trim(text), True,
                         f"{len(msgs)} message(s) via timestamp window (untagged)")

    return Slice("chat", "", False,
                 f"no attributed chat (no <!-- T{task_num} --> tags, no timestamp "
                 "window match; pass --chat-file to supply evidence)")


def collect_taskmd(task_dir: Path) -> Slice:
    """task.md layer: the declared/planned intent + gate annotations.

    Always available when the task exists — the most reliable layer.
    """
    task_file = task_dir / "task.md"
    if not task_file.exists():
        return Slice("taskmd", "", False, f"no task.md in {task_dir.name}")
    return Slice("taskmd", _trim(task_file.read_text(encoding="utf-8")),
                 True, f"{task_dir.name}/task.md")


def _git(project_path: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args], cwd=project_path,
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout


def _task_commit_range(project_path: Path, task_num: str) -> tuple[str, str] | None:
    """Best-effort commit range for a task: commits whose subject names it.

    Matches `T<NNN>` or `Task <N>` (the project's commit convention). Returns
    (base, head) where base is the parent of the earliest matching commit.
    """
    n = task_num.lstrip("0") or "0"
    # POSIX ERE (git -E) has no \b; guard the tail with a non-digit so T135
    # doesn't match T1350. Leading-boundary risk (xT135) is negligible here.
    grep = rf"(T0*{n}|[Tt]ask 0*{n})([^0-9]|$)"
    rc, out = _git(project_path, "log", "--all", f"--grep={grep}", "-E",
                   "--format=%H", "--reverse")
    if rc != 0:
        return None
    commits = [c for c in out.splitlines() if c.strip()]
    if not commits:
        return None
    first, last = commits[0], commits[-1]
    rc2, _ = _git(project_path, "rev-parse", "--verify", f"{first}^")
    base = f"{first}^" if rc2 == 0 else _EMPTY_TREE  # root commit → diff vs empty tree
    return base, last


def _has_worktree_changes(project_path: Path) -> bool:
    rc, out = _git(project_path, "status", "--porcelain")
    return rc == 0 and bool(out.strip())


def _resolve_range(project_path: Path, task_num: str,
                   base: str | None, head: str | None) -> tuple[str, str | None, str | None, str]:
    """Pick the diff source ONCE so code + tests layers stay consistent.

    Returns (kind, a, b, provenance), kind in {range, worktree, none}. Precedence:
    explicit --base/--head → commits naming the task → uncommitted worktree
    (the common "finished but not yet committed" case) → nothing.
    """
    if base and head:
        return ("range", base, head, f"--base {base} --head {head}")
    found = _task_commit_range(project_path, task_num)
    if found:
        return ("range", found[0], found[1], f"commits naming task {task_num}")
    if _has_worktree_changes(project_path):
        return ("worktree", None, None,
                f"⚠ uncommitted worktree (no commits name task {task_num})")
    return ("none", None, None,
            f"no commits name task {task_num} and worktree is clean; "
            "pass --base/--head for an explicit range")


def _worktree_diff(project_path: Path, pathspec: list[str] | None) -> str:
    """HEAD↔worktree diff for tracked files + untracked files as synthetic adds."""
    pe = (["--", *pathspec] if pathspec else [])
    _, tracked = _git(project_path, "diff", "HEAD", *pe)
    _, untracked = _git(project_path, "ls-files", "--others", "--exclude-standard", *pe)
    blocks = [tracked] if tracked.strip() else []
    for f in untracked.splitlines():
        if not f.strip():
            continue
        try:
            content = (project_path / f).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        blocks.append(f"+++ untracked: {f}\n"
                      + "\n".join("+" + ln for ln in content.splitlines()))
    return "\n".join(blocks)


def collect_code(project_path: Path, task_num: str,
                 base: str | None = None, head: str | None = None,
                 *, pathspec: list[str] | None = None,
                 layer: str = "code") -> Slice:
    """Code layer: diff for the task. Never silently the whole worktree.

    Source precedence (shared with the tests layer): explicit --base/--head →
    commits naming the task → uncommitted worktree → unavailable. `pathspec`
    restricts the diff (used for the tests layer).
    """
    kind, a, b, prov = _resolve_range(project_path, task_num, base, head)
    if kind == "none":
        return Slice(layer, "", False, f"no {layer} evidence: {prov}")
    if kind == "range":
        args = ["diff", f"{a}..{b}"] + (["--", *pathspec] if pathspec else [])
        rc, out = _git(project_path, *args)
        if rc != 0:
            return Slice(layer, "", False, f"git diff failed for {a}..{b}")
        if _has_worktree_changes(project_path):
            prov += " ⚠ dirty worktree (uncommitted changes not shown)"
    else:  # worktree
        out = _worktree_diff(project_path, pathspec)
    if not out.strip():
        what = "test files" if pathspec else "changes"
        return Slice(layer, "", False, f"no {what} ({prov})")
    return Slice(layer, _trim(out), True, prov)


def collect_tests(project_path: Path, task_num: str,
                  base: str | None = None, head: str | None = None) -> Slice:
    """Tests layer: the test-path subset of the same source as the code layer."""
    return collect_code(project_path, task_num, base, head,
                        pathspec=["tests/", "*test_*.py", "*_test.py"],
                        layer="tests")


def collect_all(project_path: Path, agent_dir: Path, task_dir: Path, task_num: str,
                *, chat_file: Path | None = None,
                base: str | None = None, head: str | None = None) -> dict[str, Slice]:
    """All four layer slices for a work unit."""
    return {
        "chat": collect_chat(agent_dir, task_num, chat_file=chat_file),
        "taskmd": collect_taskmd(task_dir),
        "code": collect_code(project_path, task_num, base, head),
        "tests": collect_tests(project_path, task_num, base, head),
    }


# ── Blind inference ──────────────────────────────────────────────────────────

_LAYER_FRAMING = {
    "chat": "the conversation between the user and the agent (raw human intent, "
            "as discussed)",
    "taskmd": "the task.md execution document (the agent's DECLARED plan and its "
              "gate-by-gate annotations)",
    "code": "the code diff that was actually committed (REALIZED intent)",
    "tests": "the test changes (ASSERTED intent — what was committed to keeping true)",
}

PROMPT_TEMPLATE = """\
You are reverse-engineering INTENT from a single source of evidence.

The evidence below is {framing}. It is the COMPLETE corpus available to you —
infer intent ONLY from it. Do NOT attempt to read any other file, run any
command, or use any tool to look beyond this text; there is deliberately nothing
else for you to find, and going outside this evidence invalidates the result.

Produce a concise report titled "Intent inferred from {layer}":
1. A bulleted list of the distinct intents/goals this evidence reveals — what
   was the work TRYING to achieve? Be specific.
2. For each intent, note your confidence (high/med/low) and the words/lines that
   support it.
3. A short "Uncertain / thin evidence" section: what you are guessing at, what
   this source is silent about. (Absence is signal — name it.)

Do not speculate about what other sources might say. Report only what THIS
evidence supports.

--- EVIDENCE ({layer}) ---
{evidence}
--- END EVIDENCE ---
"""


def build_prompt(s: Slice) -> str:
    """The blind inference prompt for one layer (evidence embedded)."""
    return PROMPT_TEMPLATE.format(
        framing=_LAYER_FRAMING.get(s.layer, s.layer),
        layer=s.layer,
        evidence=s.text,
    )


def make_default_runner(project_path: Path, *, timeout_secs: int = 300):
    """Production runner: default judge model, blindness via an evidence-only dir.

    Each call constructs the default-judge adapter with `project_root` pointed at
    a temp dir containing ONLY this layer's evidence, and an empty
    `system_context`. So even with Read/Glob/Grep the judge has nothing else to
    see — blindness is enforced by construction, not just by instruction.
    Guarantee level: strong (cwd is the evidence dir; no repo pointer) but not a
    formal jail — see task 141 OUT-of-scope (full FS isolation deferred).
    """
    import tempfile

    from provider.sandbox import load_judge_config, resolve_judge_spec
    from provider.adapters.claude import ClaudeAdapter
    from provider.adapters.codex import CodexAdapter
    from provider.adapters.antigravity import AntigravityAdapter
    from provider.adapters.pi import PiAdapter

    adapters = {"claude": ClaudeAdapter, "codex": CodexAdapter,
                "agy": AntigravityAdapter, "pi": PiAdapter}
    cfg = load_judge_config()
    provider, variant = resolve_judge_spec(cfg.get("default_judge") or "codex")
    adapter_cls = adapters.get(provider, CodexAdapter)

    def run(layer: str, prompt: str) -> str:
        with tempfile.TemporaryDirectory(prefix=f"intent-{layer}-") as td:
            (Path(td) / "evidence.md").write_text(
                prompt.split("--- EVIDENCE", 1)[-1], encoding="utf-8")
            adapter = adapter_cls(session_id="judge", project_root=Path(td))
            return adapter.run_headless_judge(
                prompt=prompt, model=variant, system_context="",
                web_search=False, timeout_secs=timeout_secs,
            )

    return run


def run_extractions(slices: dict[str, Slice], runner) -> dict[str, str]:
    """Blind-infer intent per layer. `runner(layer, prompt) -> report`.

    Unavailable layers are not sent to the model — their report records the
    provenance gap instead. `runner` is injected so tests use a fake adapter.
    """
    reports: dict[str, str] = {}
    for layer in LAYERS:
        s = slices[layer]
        if not s.available:
            reports[layer] = (f"# Intent inferred from {layer}\n\n"
                              f"_(no evidence — {s.provenance})_\n")
            continue
        reports[layer] = runner(layer, build_prompt(s))
    return reports


# ── Artifacts (additive — never overwrite prior runs or validated intent) ─────

SEAMS = [
    ("chat", "taskmd", "comprehension", "did planning capture the ask?"),
    ("taskmd", "code", "execution", "did building follow the plan?"),
    ("code", "tests", "verification", "do the assertions match what was built?"),
]


def new_run_id() -> str:
    """Timestamped run id with microseconds, e.g. 20260620-134501-281703."""
    import datetime
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def build_review(task_num: str, slices: dict[str, Slice],
                 reports: dict[str, str]) -> str:
    """The side-by-side grading sheet draft (the human fills the verdicts).

    Lays the 4 reports along the 3 seams with an empty classification skeleton —
    reconciliation/grading is the human's job, this is just the scaffold.
    """
    out = [f"# Intent review — task {task_num}", "",
           "_Vertical retro: 4 blind extractions, reconciled in-session. "
           "The model wrote the reports below; **you grade**._", "",
           "## Provenance", ""]
    for layer in LAYERS:
        s = slices[layer]
        flag = "✓" if s.available else "✗ unavailable"
        out.append(f"- **{layer}** — {flag} · {s.provenance}")
    out += ["", "## Seams (where did drift enter?)", ""]
    for a, b, name, q in SEAMS:
        out += [f"### {a} → {b} — {name}", f"_{q}_", "",
                "- [ ] aligned / [ ] drift — verdict:", ""]
    out += ["## Classification (fill during reconciliation)", "",
            "| intent | confirmed | unfulfilled | tacit | ignore |",
            "|--------|-----------|-------------|-------|--------|",
            "| | | | | |", "",
            "## Raw reports", ""]
    for layer in LAYERS:
        out += [f"### {layer}", "", reports[layer].strip(), ""]
    return "\n".join(out) + "\n"


def write_run(task_dir: Path, slices: dict[str, Slice], reports: dict[str, str],
              run_id: str | None = None) -> Path:
    """Write the 4 raw reports + review draft under intent/<run-id>/. Additive.

    Returns the run directory. Never touches prior runs.
    """
    run_id = run_id or new_run_id()
    run_dir = task_dir / "intent" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)  # never clobber a prior run
    for layer in LAYERS:
        (run_dir / f"{layer}.md").write_text(reports[layer], encoding="utf-8")
    task_num = task_dir.name.split("-", 1)[0]
    (run_dir / "review.md").write_text(
        build_review(task_num, slices, reports), encoding="utf-8")
    return run_dir


def append_intent(intent_md: Path, task_num: str, run_id: str, ratified: str) -> None:
    """Append a user-ratified entry to root INTENT.md. Append-only, never rewrites.

    Called only after the human vets (via the /intent command flow) — the CLI
    never writes INTENT.md unprompted. A stable marker keys each entry so reruns
    add new entries rather than clobbering validated intent.
    """
    marker = f"<!-- intent:T{task_num}:{run_id} -->"
    header = "# Validated Intent\n\n_Append-only. Each entry is user-ratified._\n"
    existing = intent_md.read_text(encoding="utf-8") if intent_md.exists() else header
    if marker in existing:
        return  # idempotent — this exact run already ratified
    entry = (f"\n## task {task_num} · {run_id}\n{marker}\n\n{ratified.strip()}\n")
    intent_md.write_text(existing.rstrip() + "\n" + entry, encoding="utf-8")


def last_intent_entry(intent_md: Path, task_num: str) -> str | None:
    """Most recent ratified INTENT.md entry body for a task (None if none).

    Used by the /intent flow to show the prior baseline so grading focuses on
    the delta rather than re-reviewing settled intent.
    """
    if not intent_md.exists():
        return None
    text = intent_md.read_text(encoding="utf-8")
    blocks = re.split(r"^## task ", text, flags=re.M)[1:]
    matches = [b for b in blocks if b.startswith(f"{task_num} ·")]
    if not matches:
        return None
    body = matches[-1].split("-->", 1)[-1].strip()  # drop header line + marker
    return body or None


def diff_intent(old: str, new: str) -> dict[str, list[str]]:
    """Deterministic claim-level delta between two intent texts.

    Compares bullet lines as sets — added/removed claims. A coarse first pass the
    human refines (prose intent rarely diffs cleanly), but stable enough to test.
    """
    def bullets(t: str) -> set[str]:
        return {ln.strip().lstrip("-*").strip()
                for ln in t.splitlines() if ln.strip().startswith(("-", "*"))}
    o, n = bullets(old), bullets(new)
    return {"added": sorted(n - o), "removed": sorted(o - n),
            "kept": sorted(o & n)}
