"""Composable template components for task.md files.

Each methodology clause is a function returning a markdown string.
Templates are rendered by composing components in order.

Usage:
    from tasks.template import render_template
    content = render_template(num=1, title="My Task", task_type="feature")
"""
from __future__ import annotations

from tasks.core import PLAYBOOKS


# ---------------------------------------------------------------------------
# Components — each returns a markdown string
# ---------------------------------------------------------------------------

def header(num: int, title: str) -> str:
    return f"# {num:03d} - {title}"


def sticker() -> str:
    return """\
> **Gate discipline:** One gate \u2192 do work \u2192 check box \u2192 next gate.
> Never batch. Never backfill. The document IS the execution trace.
> **Closing a gate:** check the box, append your outcome. Never replace the original text.
> Design Phase = orientation (one gate, brief answer). Work Plan = real work (one gate, full effort).
> If you see the same gate 5+ times in the hook echo, you're drifting \u2014 STOP and update."""


def status() -> str:
    return """\
## Status
pending

> **Before filling this in:** run `pb-tasks work <N>` to activate this task. Hooks won't enforce until activated."""


def intent_why_refs(playbook: str) -> str:
    return f"""\
## Intent
(what we want to achieve \u2014 the outcome, not the activity)

## Why
(why this matters now \u2014 urgency, context, what breaks if delayed)

## References
- [ ] Context: `grep -Ein "keyword1|keyword2" MIND_MAP.md` \u2192 paste relevant excerpts below
- Playbook: {playbook}
- Note: Don't hardcode task numbers in plans \u2014 `pb-tasks new` auto-increments.

---"""


def design_phase_intro() -> str:
    return """\
## Design Phase

> **Write a 1-sentence answer for each gate.** A bare checkmark means you skipped it.
> Complete these gates before writing the work plan.
> (The `/playbook` skill has workflow patterns if you need a reference.)"""


def chat_log_research() -> str:
    return """\
### Chat Log Research
- [ ] Review the "Recent Chat" messages captured in References (auto-injected at `pb-tasks work`). Remove unrelated ones. Pull key user quotes, constraints, and context into Intent/Why above. The user's actual words are the ground truth for Intent."""


def understand() -> str:
    return """\
### Understand
- [ ] Restate the request in my own words. What does the user actually want?
- [ ] Critique: Am I solving the stated problem or a different one I find more interesting?
- [ ] What would "done" look like? How will we know the task succeeded?
- [ ] What are you assuming about the existing code/architecture that you haven't verified?
- [ ] What is OUT of scope for this task?"""


def structure() -> str:
    return """\
### Structure
- [ ] What kind of work is this? (build / investigate / evaluate / decide / combination?) If combination, what's the sequence? If >15 gates or uncertain approach, pick a checkpoint where you pause and reassess direction before continuing."""


def reflection_gates() -> str:
    return """\
### Reflection Gates
- [ ] Wrote task-specific check questions (Bad: "is this working?" Good: "Does the output include the progress counter?" \u2014 the answer should require evidence, not just yes/no)
- [ ] Test strategy: what are you testing and how? (point tests for specific behavior, property tests via `hypothesis` for invariants on transformations/parsers/arithmetic)
- [ ] Before the riskiest step: what would make you stop and reconsider?
- [ ] If judging quality before building: is the gap worth closing?"""



def verify() -> str:
    return """\
### Verify
- [ ] Review the work plan. If a likely growth point exists, add it to the plan now.
- [ ] Does the work plan include moments where you stop and question your approach \u2014 not just execute?
- [ ] Checkpoint: Would a fresh agent understand this task and execute it well?
- [ ] The work plan below has the right granularity (not too coarse, not micro-steps)"""


def design_phase() -> str:
    """Compose all design phase subsections."""
    parts = [
        design_phase_intro(),
        chat_log_research(),
        understand(),
        structure(),
        reflection_gates(),
        verify(),
    ]
    return "\n\n".join(parts)


def judge_section() -> str:
    return """\
## Plan Review
- [ ] Run `pb-tasks plan-review <N>` — wait for it to finish (it edits this file). Re-read this file to see its findings below, then address valid concerns by revising Work Plan gates. **Justify lens:** does every work gate trace up to something in Intent/Design? Are there gates that justify nothing above them (scope creep)? Intent claims with no gate to satisfy them (gaps)?
- [ ] **Triage plan-review findings: judge = opinion, not gospel.** For each finding, document accept (with rationale) / park (with rationale) / reject (with rationale). Push back where you have concrete evidence — you live with the outcomes, the reviewer doesn't. Verify file:line claims before applying — single-judge reviews can cite wrong locations.
- [ ] *(Optional)* Run `pb-tasks panel-review <N>` for multi-model panel (writes to judge.md, not this file). Add `--prompt "..."` to append extra steering (e.g. focus area, constraint). Read judge.md with user, accept/reject findings, apply selected advice to Work Plan.

(plan review findings appear here)

---"""


def work_plan() -> str:
    return """\
## Work Plan

> For each work section: what could go wrong? How will you know it worked? (specific check, not "looks good")
> Standard feature: 6-8 work gates + tests. Large tasks work fine — if >15 gates, add a mid-point checkpoint to reassess direction.

(write work gates here)

---"""


def judge_impl_section() -> str:
    return """\
## Implementation Review
- [ ] Run `pb-tasks impl-review <N>` — wait for it to finish (it edits this file). Re-read findings. **Satisfy lens:** does every Intent claim trace down through code to tests? Where does the chain break?
- [ ] **Triage impl-review findings: judge = opinion, not gospel.** For each finding, document accept (with rationale) / park (with rationale) / reject (with rationale). Push back where you have concrete evidence — you live with the outcomes, the reviewer doesn't. Verify file:line claims before applying — single-judge reviews can cite wrong locations.
- [ ] *(Optional)* Run `pb-tasks panel-review <N> --mode impl` for multi-model panel review. Add `--prompt "..."` to append extra steering.

(implementation review findings appear here)

---"""


def debrief() -> str:
    return """\
## Debrief
- [ ] Freehand — work is done, stay for discussion with user. Remove this gate during Design Phase if running headless or task doesn't need debrief."""


def pre_review() -> str:
    return """\
## Pre-review
- [ ] All tests pass
- [ ] No debug artifacts
- [ ] MIND_MAP.md updated if new insights emerged"""


def parked() -> str:
    return """\
## Parked
(Findings or ideas that emerged during work but are out of scope. Describe each with enough context for a future task to pick it up.)

---"""


def _intent_check(task_path: str) -> str:
    """Extract task number and return intent-check instruction for judge prompts."""
    import re as _re
    _tn = _re.search(r'[/\\](\d{3})-', task_path)
    task_number = _tn.group(1) if _tn else None
    if task_number:
        return (
            f"If .agent/chat_log.md exists, run `pb-tasks context {task_number}` to see the user's original messages. "
            "Check whether the task addresses what the user actually asked for, not just the agent's interpretation. "
        )
    return ""


def plan_review_prompt(task_path: str, inline_context: bool = False) -> str:
    """Return the blind judge prompt for plan review (before implementation)."""
    context_location = "provided below" if inline_context else "provided in your system prompt"
    intent_check = _intent_check(task_path)

    return (
        "You are a senior engineer reviewing a PLAN — no code has been written yet. "
        f"The MIND_MAP.md and task.md are {context_location}. "
        "Read the source files referenced in the plan to understand existing patterns. "
        f"{intent_check}"
        "Then critique the plan through five lenses: "
        "(1) Intent alignment — will this approach actually fulfill the stated Intent? What's missing or underspecified? "
        "(2) Failure modes — what will go wrong that isn't addressed? Construct a concrete failing scenario. "
        "(3) Test coverage — does the test plan cover the failure modes above? For pure-function code, does it identify invariants (idempotency, bounds, round-trip) worth property-testing? "
        "(4) Simplify — is anything over-engineered? What can be dropped? "
        "(5) Prove it — cite file:line evidence for claims about existing code. No hand-waving. "
        "Be specific and adversarial — your job is to find problems, not approve. "
        "Max 5 findings, Critical and Important only — drop Minor. "
        "Each finding: cite file:line, 1-2 sentences stating the problem, 1 sentence stating the fix. No elaboration. "
        "Return only the bounded review findings in your final response. "
        f"Do not edit or claim {task_path}, run pb-tasks work, or mutate any project file. "
        "The harness saves your response as a separate review artifact; the owning "
        "session verifies the evidence, triages each finding, publishes accepted "
        "findings into ## Plan Review, and revises ## Work Plan gates."
    )


def impl_review_prompt(task_path: str, inline_context: bool = False) -> str:
    """Return the blind judge prompt for implementation review (after code is written)."""
    context_location = "provided below" if inline_context else "provided in your system prompt"
    intent_check = _intent_check(task_path)

    return (
        "You are a senior engineer reviewing a COMPLETED implementation. "
        f"The MIND_MAP.md and task.md are {context_location}. "
        "Read the source files changed by this task (look at the Work Plan gates for paths). "
        f"{intent_check}"
        "Review through five lenses: "
        "(1) Simplify — what's unnecessary or over-engineered? What can be removed? "
        "(2) Self-critique — does the code actually fulfill the stated Intent? What would a skeptic say? "
        "(3) Bug scan — find actual bugs, edge cases, race conditions, or security issues. "
        "(4) Test quality — do the tests verify Intent claims or just confirm the implementation? For pure-function code (parsers, formatters, transformations), are there untested invariants that property tests would catch? "
        "(5) Prove it works — cite file:line evidence showing correctness, or construct a concrete scenario showing failure. "
        "Be specific and adversarial — your job is to find problems, not approve. "
        "Max 5 findings, Critical and Important only — drop Minor. "
        "Each finding: cite file:line, 1-2 sentences stating the problem, 1 sentence stating the fix. No elaboration. "
        "Return only the bounded review findings in your final response. "
        f"Do not edit or claim {task_path}, run pb-tasks work, or mutate any project file. "
        "The harness saves your response as a separate review artifact; the owning "
        "session verifies the evidence, triages each finding, and publishes accepted "
        "findings into ## Implementation Review through the task authority path."
    )


def panel_plan_review_prompt(task_path: str, inline_context: bool = False) -> str:
    """Panel judge prompt for plan review — writes to stdout, never edits task.md."""
    context_location = "provided below" if inline_context else "provided in your system prompt"
    intent_check = _intent_check(task_path)

    return (
        "You are a senior engineer reviewing a PLAN — no code has been written yet. "
        f"The MIND_MAP.md and task.md are {context_location}. "
        "Read the source files referenced in the plan to understand existing patterns. "
        f"{intent_check}"
        "Then critique the plan through five lenses: "
        "(1) Intent alignment — will this approach actually fulfill the stated Intent? What's missing or underspecified? "
        "(2) Failure modes — what will go wrong that isn't addressed? Construct a concrete failing scenario. "
        "(3) Test coverage — does the test plan cover the failure modes above? For pure-function code, does it identify invariants (idempotency, bounds, round-trip) worth property-testing? "
        "(4) Simplify — is anything over-engineered? What can be dropped? "
        "(5) Prove it — cite file:line evidence for claims about existing code. No hand-waving. "
        "Be specific and adversarial — your job is to find problems, not approve. "
        "Max 5 findings, Critical and Important only — drop Minor. "
        "Each finding: cite file:line, 1-2 sentences stating the problem, 1 sentence stating the fix. No elaboration. "
        "Note: your findings will be triaged by the reading agent — they will verify file:line claims before applying, push back on speculative concerns, and require concrete evidence. Self-flag any claim you cannot defend with code citation. The reading agent lives with the outcomes; you do not. "
        "DO NOT edit any files. Output your findings to stdout only."
    )


def panel_impl_review_prompt(task_path: str, inline_context: bool = False) -> str:
    """Panel judge prompt for impl review — writes to stdout, never edits task.md."""
    context_location = "provided below" if inline_context else "provided in your system prompt"
    intent_check = _intent_check(task_path)

    return (
        "You are a senior engineer reviewing a COMPLETED implementation. "
        f"The MIND_MAP.md and task.md are {context_location}. "
        "Read the source files changed by this task (look at the Work Plan gates for paths). "
        f"{intent_check}"
        "Review through five lenses: "
        "(1) Simplify — what's unnecessary or over-engineered? What can be removed? "
        "(2) Self-critique — does the code actually fulfill the stated Intent? What would a skeptic say? "
        "(3) Bug scan — find actual bugs, edge cases, race conditions, or security issues. "
        "(4) Test quality — do the tests verify Intent claims or just confirm the implementation? For pure-function code (parsers, formatters, transformations), are there untested invariants that property tests would catch? "
        "(5) Prove it works — cite file:line evidence showing correctness, or construct a concrete scenario showing failure. "
        "Be specific and adversarial — your job is to find problems, not approve. "
        "Max 5 findings, Critical and Important only — drop Minor. "
        "Each finding: cite file:line, 1-2 sentences stating the problem, 1 sentence stating the fix. No elaboration. "
        "Note: your findings will be triaged by the reading agent — they will verify file:line claims before applying, push back on speculative concerns, and require concrete evidence. Self-flag any claim you cannot defend with code citation. The reading agent lives with the outcomes; you do not. "
        "DO NOT edit any files. Output your findings to stdout only."
    )


# Legacy alias for backward compatibility
def judge_prompt(task_path: str, inline_context: bool = False,
                 mode: str = "plan") -> str:
    """Deprecated: use plan_review_prompt() or impl_review_prompt() instead."""
    if mode == "impl":
        return impl_review_prompt(task_path, inline_context)
    return plan_review_prompt(task_path, inline_context)


def design_phase_light() -> str:
    """Lightweight design phase for Fix tasks — just restate and define done."""
    return "## Design Phase\n\n" + chat_log_research() + "\n\n" + """\
### Fix Orientation
- [ ] What exactly is broken or needs cleaning up?
- [ ] What does "fixed" look like? (specific grep, test, or behavior)
- [ ] What adjacent code could this break?
- [ ] Test strategy: point tests, or also property tests (`hypothesis`) if fixing a parser/formatter/transformation?"""


def work_plan_fix() -> str:
    """Fix-specific work plan — locate, fix, verify pairs."""
    return """\
## Work Plan

> Fix/Verify pairs. What could this break?

- [ ] Fix: (what to change)
- [ ] Verify: (grep/test that confirms the fix)
- [ ] Side effects: anything else that changed? Adjacent code still works?

---"""


def design_phase_investigate() -> str:
    """Investigate-oriented design phase — hypothesis-first."""
    return "## Design Phase\n\n" + chat_log_research() + "\n\n" + """\
### Investigation Orientation
- [ ] What's the question or hypothesis? State it before looking.
- [ ] What evidence would change your mind?
- [ ] When do you stop? (convergence criteria: N rounds with no new position, or specific answer found)
- [ ] Test strategy: if findings lead to code changes, point tests or also property tests (`hypothesis`) for invariants?"""


def work_plan_investigate() -> str:
    """Investigate-specific work plan — round structure."""
    return """\
## Work Plan

> Rounds: hypothesis → test → result → checkpoint. Stop when converging.

### Round 1: [focus]
- **Hypothesis:** (before testing)
- **Test:** (what to check)
- **Result:** (what happened)
- [ ] Checkpoint: converging or scattering? New hypothesis needed?

### Round 2: [focus]
- **Hypothesis:** (refined from Round 1)
- **Test:** (what to check)
- **Result:** (what happened)
- [ ] Checkpoint: converging or scattering?

### Synthesis
- [ ] What did you learn? Key findings with evidence.
- [ ] What remains unknown? What would a follow-up task investigate?

---"""


def design_phase_evaluate() -> str:
    """Evaluate-oriented design phase — define lenses and scope."""
    return "## Design Phase\n\n" + chat_log_research() + "\n\n" + """\
### Evaluation Orientation
- [ ] What are you evaluating, and against what criteria?
- [ ] Define lenses (2-4 dimensions to assess consistently across all items)
- [ ] How many items? If >5, plan a midpoint checkpoint.
- [ ] Are you assessing or fixing? Keep them separate — assess first.
- [ ] Test strategy: if evaluation leads to fixes, point tests or also property tests (`hypothesis`) for invariants?"""


def work_plan_evaluate() -> str:
    """Evaluate-specific work plan — lenses, per-item, verdict."""
    return """\
## Work Plan

> Apply lenses consistently. Assess first, decide action after.

### Lenses
| Lens | What it measures |
|------|-----------------|
| (lens 1) | (description) |
| (lens 2) | (description) |

### Assessment
- [ ] Item 1: (apply all lenses)
- [ ] Item 2: (apply all lenses)
- [ ] Midpoint checkpoint: patterns emerging? Abort early or continue?

### Verdict
- [ ] Overall assessment: PASS / PARTIAL / FAIL
- [ ] Gaps found: cosmetic or material?
- [ ] Sufficiency: is the current state good enough, or do gaps justify action?

---"""


def standing_orders() -> str:
    return """\
## Standing Orders
- **Expand dynamically**: When you discover something you'll need to do, write new gates immediately \u2014 don't wait until you get there.
- **Steer openly**: If your direction changes, edit your open (unchecked) gates to reflect reality. The plan is alive, not a contract.
- **Never defer awareness**: The moment you realize work exists, capture it. Forgetting is the failure mode, not having too many gates."""


def runtime_identity_guidance() -> str:
    """Recipient-side fail-safe when an older ambient CLI serves newer guidance."""
    return """\
Bootstrap must print a `Playbook runtime:` line (version, commit, executable,
and root) plus `Project generation:`. If either line is absent, or generation
reports `SKEW`, stop before task ownership changes. Run `command -v pb-tasks`
and `pb-tasks runtime-info`; then upgrade the ambient launcher or rerun init
from the intended runtime. Historical task `## Sessions` rows are ownership
history, never evidence of this process's native identity."""


# ---------------------------------------------------------------------------
# CLAUDE.md init template
# ---------------------------------------------------------------------------

def claude_md(title: str) -> str:
    """Generate CLAUDE.md content for `pb-tasks init`."""
    return f"""\
# {title}

## Start Here

```bash
pb-tasks bootstrap          # loads mind map, skills, pending tasks
```

Then **ask the user** what they want to work on. Don't autonomously pick a task.

{runtime_identity_guidance()}

## When a Task Is Required

Start or activate a task **only for repository code work**. A task is not
required for plans or Markdown documentation, read-only review, operational
shell work that does not edit repository code, or starting/stopping services.
If operational work leads to a code change, activate a task before that edit.

Before creating a task, inspect the most recent 2–3 tasks and cluster matching
work into the related task: reopen it with `pb-tasks work <N>` and append unchecked gates.
Create a new task only when none of those recent tasks honestly matches.

## Enforcement Scope

The workflow rule is broader than hook coverage: activate a task before every
repository code edit. Hooks recognize configured code paths and validate
task.md gate order through structured editor calls. Shell-command detection is
heuristic and bypassable; only sandbox containment can physically deny an
arbitrary shell write. An active task authorizes repository code edits but does
not prove that an edit semantically belongs to the current gate.

## CLI

```bash
pb-tasks work <number>              # activate task, hook starts tracking
pb-tasks work done [--force]        # finish after gates + intent reconcile; open gates bounce
pb-tasks new <type> <name> [intent] # create task — intent fills ## Intent
pb-tasks new --stub <type> <name> [intent] # stub — expands on pb-tasks work
pb-tasks plan-review <number>       # blind plan review by independent agent
pb-tasks impl-review <number>       # blind implementation review by independent agent
pb-tasks list [--pending|--recent]  # all, open, or latest 3 tasks
pb-tasks status                     # current gate position
pb-tasks bootstrap                  # orientation: mind map + skills + pending
pb-sandbox --prompt "..." [--agent claude|codex|agy|pi] [--bare]  # run a contained headless subagent; `--help` for flags
```

## Don't

- Create task directories manually — always `pb-tasks new`
- Edit `.agent/sessions/` state files directly — use `pb-tasks work <N>` / `pb-tasks work done`
- Edit `## Status` in task.md directly — use `pb-tasks work done`
- Close a gate other than the exact current task.md gate supplied by the hook, or close multiple gates in one edit
- Use shell redirection to bypass task ownership or gate-order validation; structured hooks mediate editor writes, while only sandbox containment can physically deny arbitrary shell writes
- Skip task.md checkboxes — they're your observable progress
- Start coding without an active task — run `pb-tasks work <N>` first; hook coverage has the limits above
- Use EnterPlanMode or plan files — use `pb-tasks new <type> <name>` instead, the task.md IS the plan
"""


# ---------------------------------------------------------------------------
# Bootstrap briefing
# ---------------------------------------------------------------------------

def identity_preamble() -> str:
    """Framing shown at the top of bootstrap."""
    return (
        "You are a coding assistant working with a task management harness.\n"
        "Bootstrap is orientation, not authorization: inspect this context, then "
        "stop and ask the user what to work on. Do not select or activate a task."
    )


def mind_map_header() -> str:
    """Navigation header shown before full mind map at bootstrap."""
    return (
        "Project knowledge graph. Nodes cross-reference with [N] IDs.\n"
        "Full map below — drill into a node: grep '^\\[N\\]' MIND_MAP.md\n"
        "Format spec: /mindmap skill"
    )



def workflow_briefing() -> str:
    """Workflow rules shown at task activation (pb-tasks work <N>)."""
    return """\
- One gate at a time: read gate → do work → check box → next gate
- Pattern templates in task.md ARE the work plan — fill them in, don't skip"""


def cli_reference() -> str:
    """Full public CLI reference shown at bootstrap and in provider guidance.

    ``pb-tasks --help`` is the authority. Reusing it here prevents bootstrap
    and generated onboarding files from maintaining a second command list.
    """
    return usage_text()


def agents_md_template() -> str:
    """AGENTS.md content for Codex projects.

    Codex auto-loads AGENTS.md from the repo root (baked into its base
    instructions).  This file teaches the agent the Playbook workflow.
    Embed cli_reference() literally — current at install time.  To refresh
    after a Playbook upgrade: delete AGENTS.md, then re-run
    `pb-tasks init --provider codex`.
    """
    return """\
# Playbook Workflow

This project uses the **Playbook task harness**.  Follow these rules on every
session — they govern how you work, not what you build.

## Start of Session

Run this first, before anything else:

    pb-tasks bootstrap

It prints the project mind map, pending tasks, and the full CLI reference.
Read it.  Then stop and ask the user what to work on. Bootstrap is orientation,
not authorization to select or activate a pending task.

{runtime_identity}

## Before Editing Code

You **must** activate a task before touching any code file:

    pb-tasks work <N>      # e.g. pb-tasks work 042

This sets the active task. Without it, recognized `apply_patch` code edits are
blocked; the enforcement limits below still apply.

## When a Task Is Required

Start or activate a task **only for repository code work**.  A task is not
required for plans or Markdown documentation, read-only review, operational
shell work that does not edit repository code, or starting/stopping services.
If operational work leads to a code change, activate a task before that edit.

Before creating a task, inspect the most recent 2–3 tasks and cluster matching
work into the related task: reopen it with `pb-tasks work <N>` and append unchecked gates.
Create a new task only when none of those recent tasks honestly matches.

## Codex Enforcement Scope

The workflow rule is broader than hook coverage: activate a task before every
repository code edit. Pre-edit checks cover `apply_patch` on configured code
paths. Shell writes bypass that precheck and are not attributed by Playbook:
shared-worktree state cannot prove which session authored them. Only sandbox
containment can physically deny arbitrary shell writes. An active task allows
repository-wide patches but does not prove that an edit semantically belongs
to the current gate. Owned task.md patches separately enforce one-gate-at-a-time
progression. Stop still enforces unfinished gates for an attributable active task.

## Working Through a Task

- Read the task.md that `pb-tasks work` prints.
- Work **one gate at a time**: follow the gate and exact task.md route supplied
  by the hook → do the work → use your normal file-edit tool to check that one
  box and append your outcome on the same line. The hook then supplies the next gate.
- Never skip gates.  Never batch-close multiple gates in one edit.
- If you discover new work, add new gates to task.md immediately.

## End of Task

    pb-tasks work done

This deactivates the task and marks it done. Run it explicitly when all gates
are checked and the task intent is honestly reconciled — not before. Do not
rely on a later task switch to close completed work.

## CLI Reference

{cli_ref}

## Do Not

- Edit `.agent/sessions/` files directly — use `pb-tasks work` / `pb-tasks work done`.
- Create `.agent/tasks/NNN-name/` directories manually — use `pb-tasks new`.
- Close a gate other than the exact current task.md gate supplied by the hook, or close multiple gates in one edit.
- Use shell redirection to bypass task ownership or gate-order validation; use your provider's structured file-edit tool. Only sandbox containment can physically deny arbitrary shell writes.
- Close multiple gates in a single edit.
- Start coding without an active task.
""".format(
        cli_ref=cli_reference(), runtime_identity=runtime_identity_guidance()
    )


def antigravity_md_template() -> str:
    """GEMINI.md content for Antigravity CLI (`agy`) projects.

    agy reads GEMINI.md from project cwd (mirrors the user-level `~/.gemini/GEMINI.md`
    convention) and can also load AGENTS.md. Standalone project init installs
    guidance without claiming or installing Agy hook enforcement.

    Model selection: current agy accepts `--model`; panel-review pins each Gemini
    judge explicitly. Interactive sessions may still use the model selected in
    the agy UI when no flag is supplied.
    """
    return """\
# Playbook Workflow

This project uses the **Playbook task harness**. Agy may load both `AGENTS.md`
and `GEMINI.md`. Shared workflow in `AGENTS.md` still applies, but this file is
the provider-specific authority for Agy capabilities: Codex or other-provider
hook/enforcement claims in `AGENTS.md` do not describe Agy.

Standalone `pb-tasks init` installs this guidance without installing or claiming
Agy hooks. Treat enforcement as advisory unless the active Agy integration
reports an installed capability separately. A previously installed user-global
plugin can still be active; inspect `agy plugin list` for its provenance.

## Start of Session

Run this first:

    pb-tasks bootstrap

It prints the project mind map, pending tasks, and the full CLI reference.
Read it. Then stop and ask the user what to work on. Do not select or activate
a pending task without that direction.

{runtime_identity}

## Before Editing Code

Activate a task:

    pb-tasks work <N>

## Working Through a Task

Work one gate at a time.  Check each gate box before moving to the next.
Use your normal structured file-edit tool on the exact task.md gate supplied by
the hook. The hook rejects foreign, skipped, or multi-gate closure and then
supplies the next gate; no gate-edit CLI command is part of the workflow.

## End of Task

    pb-tasks work done

Run it explicitly only after all gates are checked and the task intent is
honestly reconciled. Do not rely on a later task switch to close completed work.

## CLI Reference

{cli_ref}

## Do Not

- Edit `.agent/sessions/` files directly — use `pb-tasks work` / `pb-tasks work done`.
- Create `.agent/tasks/NNN-name/` directories manually — use `pb-tasks new`.
- Use shell redirection to bypass task ownership or gate-order validation.
- Close multiple gates in a single edit.
- Start coding without an active task.
""".format(
        cli_ref=cli_reference(), runtime_identity=runtime_identity_guidance()
    )


# ---------------------------------------------------------------------------
# CLI usage
# ---------------------------------------------------------------------------

def usage_text() -> str:
    """Usage text for `tasks --help`."""
    types = ", ".join(sorted(set(PLAYBOOKS.keys()) | {"quick"}))
    return f"""\
Usage: pb-tasks <command> [args]

First decisions:
  work <number>       Set active task (e.g. pb-tasks work 058)
                      Claimed task: use exactly one explicit owner proof:
                        --handoff-from provider:native-id  cooperative transfer
                        --recover-from provider:native-id  abandoned-owner recovery
  work done [--force] Finish task; bounces if gates still open (--force overrides)
  freehand            User-driven mode (no gate pressure)
  new <type> <name> [intent]   Create task (intent pre-fills ## Intent)
  new --stub <type> <name> [intent]   Create stub (expands on work)
  list [--pending|--recent] List all, open, or latest 3 tasks with status
  status              Show head position for active tasks
  bootstrap           Orientation: mind map, skills, tasks, recent messages, this reference

Review and analysis:
  plan-review <N>     Run blind plan review
  impl-review <N>     Run blind implementation review
  panel-review [<N>]  Multi-model judge panel
                      --prompt "..."     add steering (appended to review prompt, or full mission if no task)
                      --no-mind-map      strip mind map from context
                      --bare             no context at all; --prompt is the entire prompt
  retro [--since N]   Project retrospective
  intent <N> [--collect-only] [--base REF --head REF]
                      Collect and compare task intent across evidence layers
  global-retro-collect --since DATE [--machine NAME] [--out DIR] [--format zip|tgz] ROOT [ROOT...]
                      Collect Playbook artifacts for a global retro archive
  context <N>         Extract chat messages for a task
  log [N] [--width W]  Compact one-line-per-message chat log (last N, body cropped to W; default all/500)
  narrative [--status|--pending|--render] [--lines N] [--limit N]
                      Read the chat log as arcs over spans over comments, for the
                      user to see at a glance what happened over hours.
                      --status (default) what is narrated, what is new
                      --pending           un-narrated comments, one line each
                      --render            write .agent/narrative/narrative.html
                      Annotations are authored by the /narrative skill, never inferred.

Maintenance:
  prepare-merge [--target <branch>] [--dry-run]
                      Renumber tasks, re-sequence chat_log, report MIND_MAP collisions
                      so the branch merges cleanly into target (default: main)
  doctor              Harness health check
  init [PROJECT] [--provider NAME] [--no-hooks]
                      Reconcile local Harness files for installed agents

Sandboxed subagents (separate CLI):
  pb-sandbox --prompt "..." [--agent claude|codex|agy|pi] [--bare] [--stream]
                      Run a contained headless agent (write-containment)
  pb-sandbox --help   Discover models, providers, policy, and all sandbox flags

Task types: {types}

Examples:
  pb-tasks work 058
  pb-tasks new feature add-auth
  pb-tasks new build my-task Build extraction layer for retro command
  pb-tasks new --stub research token-bug Investigate auth token refresh
  pb-tasks plan-review 001
  pb-tasks panel-review 001 --prompt "focus on the title-detection approach"
  pb-tasks panel-review --prompt "which of these two designs is simpler?" --no-mind-map
  pb-tasks panel-review --bare --prompt "read ideas.txt and pick the best story idea"
  pb-tasks global-retro-collect --since 2026-03-14 ~/Code /data --out /tmp
  pb-tasks list --pending
  pb-tasks list --recent"""


# ---------------------------------------------------------------------------
# Composition
def sticker_quick() -> str:
    return """\
> **Gate discipline:** One gate \u2192 do work \u2192 check box \u2192 next gate.
> Never batch. Never backfill. The document IS the execution trace."""


def render_stub_template(num: int, title: str, intent_text: str = "",
                         task_type: str | None = None) -> str:
    """Minimal stub for GTD capture. No gates, expands on `pb-tasks work <N>`."""
    type_tag = task_type or "feature"
    parts = [
        header(num, title),
        f"<!-- stub:{type_tag} -->",
        status(),
        f"## Intent\n{intent_text}" if intent_text else "## Intent\n(fill in before expanding)",
        "## Why\n(fill in before expanding)",
        "## References\n(optional)",
    ]
    return "\n\n".join(parts) + "\n"


def render_quick_template(num: int, title: str) -> str:
    """Minimal task.md for sub-hour fixes and small work. ~3 gates, no ceremony."""
    parts = [
        header(num, title),
        sticker_quick(),
        status(),
        "## Intent\n(one line — what to do and how to verify)",
        "---",
        "## Work\n- [ ] Do the work\n- [ ] Test: verify it worked\n- [ ] Cleanup: mind map, commit",
        "## Parked\n(out of scope discoveries)",
    ]
    return "\n\n".join(parts) + "\n"


def render_monitor_template(num: int, title: str) -> str:
    """Long-lived blackboard for intent and asynchronously completing lanes."""
    return f"""\
{header(num, title)}

<!-- playbook-task-mode: monitor-board -->

> **Monitor board:** Capture new work immediately. Dispatch from lane sections.
> Close an assignment gate only after collection and validation; append the report.
> Lane gates may close in the order events arrive. Keep the board useful after compaction or reboot.

- Playbook: playbook/Monitor

{status()}

## Intent
(what we want to achieve — the outcome, not the activity)

### Goals and success criteria
(desired outcomes and how the user will recognize success)

### Constraints and decisions
(authority, boundaries, explicit decisions, and superseded decisions)

### User struggle
(what is difficult or costly for the user; preserve their words when meaning matters)

### Unresolved questions
(questions still under discussion; do not dispatch guesses)

## Why
(why this matters now)

## Incoming Work

Add every new request here immediately as an open gate. Capture first; organize by priority and dependencies separately. Move or link it to a lane section when dispatched.

## Watching and Recovery

- [ ] Establish and verify watching: mechanism, scope, cadence, watch handle, coverage limits, and a received test event. After restart, reconcile live state, re-arm the watcher, and verify it again.

Quiet is an inspection signal, not proof of a stall. Record watcher failure or lost visibility separately. A persistent objective can continue the monitoring goal; it does not provide timed wakes.

## Lanes

### lane-name

- [ ] Capture recovery details: lane name; provider; native session ID; model; current task; resume directory.

Add an assignment gate when sending work. Include outcome and constraints. Close it after collection and validation with the result, evidence, judgment, and remaining work. Corrections get new gates.

## Cross-lane Work

(dependencies, shared resources, ownership conflicts, and comparisons that no lane can see alone)

## Intent Review

(Record fresh intent reviews when useful: source messages first, then compare the plan and delivery. Triage findings; judges advise.)

## Decisions and Reports

(Short reports, consequential judgments, user verdicts, uncertainties, and monitor mistakes.)

## End

- [ ] End monitoring: reconcile intent and incoming work, collect or hand off every lane, stop or transfer watching, record recovery state, and report remaining uncertainty.

## Parked

(Out-of-scope discoveries with enough context to recover later.)
"""


# ---------------------------------------------------------------------------

def render_template(num: int, title: str, task_type: str | None = None) -> str:
    """Compose all components into a complete task.md template.

    Args:
        num: Task number (will be zero-padded to 3 digits)
        title: Task title (will be title-cased in header)
        task_type: Optional task type for playbook reference

    Returns:
        Complete task.md content as a string
    """
    # Quick template — standalone, no PLAYBOOKS lookup
    if task_type == "quick":
        return render_quick_template(num, title)
    if task_type == "monitor":
        return render_monitor_template(num, title)

    pattern_name = PLAYBOOKS.get(task_type) if task_type else None
    playbook_ref = f"playbook/{pattern_name}" if pattern_name else "(none)"

    # Common parts shared by all variants
    common_start = [
        header(num, title),
        sticker(),
    ]
    common_start += [
        status(),
        intent_why_refs(playbook_ref),
    ]
    common_end = [
        debrief(),
        pre_review(),
        parked(),
        standing_orders(),
    ]

    if pattern_name == "Fix":
        middle = [
            design_phase_light(),
            work_plan_fix(),
        ]
    elif pattern_name == "Investigate":
        middle = [
            design_phase_investigate(),
            work_plan_investigate(),
        ]
    elif pattern_name == "Evaluate":
        middle = [
            design_phase_evaluate(),
            work_plan_evaluate(),
        ]
    else:
        # Build (default) — full ceremony
        middle = [
            design_phase(),
            judge_section(),
            work_plan(),
            judge_impl_section(),
        ]

    parts = common_start + middle + common_end
    return "\n\n".join(parts) + "\n"
