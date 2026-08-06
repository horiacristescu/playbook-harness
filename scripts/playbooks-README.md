# Custom Playbook Templates

Drop a `.md` file in this folder and it becomes a task type in `pb-tasks new`.

## How it works

```bash
pb-tasks new sp-eval my-eval-task    # uses .agent/playbooks/sp-eval.md
pb-tasks new feature my-feature      # uses built-in Python template (no .md needed)
```

If `.agent/playbooks/<type>.md` exists, it's used instead of the base template. If not, the built-in template from `src/tasks/template.py` is used.

## Substitution variables

Two variables are replaced when stamping a template into a new task:

| Variable | Replaced with | Example |
|----------|--------------|---------|
| `{{NNN}}` | Zero-padded task number | `077` |
| `{{TITLE}}` | Title-cased task name | `My Eval Task` |

## Required sections

A playbook template **must** have:

- `## Status` followed by `pending` on the next line — hooks use this to track task lifecycle
- `## Intent` — even if just a placeholder `(fill in)`, this section is read by `pb-tasks list`
- At least one `- [ ]` gate — hooks track progress via checkbox counting

## What makes a good template

**Gates should be actionable, not aspirational.** Each `- [ ]` should describe a concrete action the agent can complete and check off. Bad: `- [ ] Think about the problem`. Good: `- [ ] Run preflight check, confirm backend is reachable`.

**Match ceremony to task shape.** The base template has Design Phase, Plan Review, Implementation Review — that's right for features and refactors. An eval/orchestration template needs different sections (Planned Runs, Execution blocks, Diversity Tags). A fix template might need only Intent + Work Plan.

**Include domain knowledge inline.** The sp-eval template embeds CLI commands, budget guidelines, failure modes, and verification methods. An agent using the template gets the accumulated knowledge without reading separate docs. This is the template's main value — it's a compressed expert.

**Use the sticker.** The first lines after the title should be a blockquote with gate discipline instructions. This is what the agent sees every time it opens the file:

```markdown
> **Gate discipline:** One gate → do work → check box → next gate.
> Never batch. Never backfill. The document IS the execution trace.
```

**End with a direction note for dynamic expansion.** If agents will add gates during execution:

```markdown
- [x] **DIRECTION NOTE** — Add new gates above this line.
```

## Example structure

```markdown
# {{NNN}} - {{TITLE}}

> Gate discipline sticker here.

## Status
pending

## Intent
(what this task achieves)

## Domain-Specific Section
(reference material the agent needs — commands, patterns, failure modes)

## Planned Work
- [ ] Gate 1: concrete action
- [ ] Gate 2: concrete action
- [ ] Checkpoint: is the approach working?

## Parked
(out-of-scope findings go here)
```

## Existing templates

- **sp-eval.md** — ScreenPlay CUA evaluation: orchestrate GUI interaction traces. Sections: Orchestrator Mindset, Interaction Primitives, App Task Space, CLI Reference, Exec Modes, Failure Modes, Verification, then per-exec gate blocks (Observe → Run → Judge).
