---
description: Vertical retro — infer intent blind from a task's 4 layers, reconcile with the user, distill to INTENT.md
argument-hint: "<task-number> [--base REF --head REF] [--chat-file PATH]"
allowed-tools: [Read, Bash, Edit, Grep, Glob]
---
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/intent.md","source_hash":"e793a20e0e444abfb474bded41ea5af023dc2413bfb8170ad4d4c38b7f5fe5e4"} -->

# Intent Review (vertical retro)

Reverse-engineer the **intent** of one work unit by inferring it independently —
*blind* — from its four layers (chat → task.md → code → tests), then reconcile
the four reports **with the user** and distill what they vet into `INTENT.md`.

This is the depth-wise sibling of `/retro` (which is horizontal, across tasks).
The model only does the 4 blind extractions; **the user grades** — you are the
reconciler, not the judge. Independent axis = (chat + task.md) ↔ (code + tests).

**Task:** $ARGUMENTS

## Steps

### 1. Run the blind extractions

```bash
pb-tasks intent $ARGUMENTS
```

This fans out up to 4 isolated judge calls (default judge model, evidence-only
sandbox per layer) and writes `.agent/tasks/NNN-*/intent/<run-id>/` containing
`chat.md taskmd.md code.md tests.md review.md`. Note any layer reported `✗
unavailable` — that gap is itself signal, not a failure to paper over. If code
evidence is missing, ask the user for `--base/--head` refs and re-run.

### 2. Read the raw reports

Read all four layer reports **and** `review.md`. The user should be able to read
the raw reports themselves — do not summarize them away; surface them.

### 3. Reconcile along the three seams (with the user)

Walk the user through each seam — these localize *where* intent fractured:

- **chat → task.md (comprehension)** — did planning capture the ask?
- **task.md → code (execution)** — did building follow the plan?
- **code → tests (verification)** — do assertions match what was built?

For each divergence, classify with the user:
- **confirmed** — stated and realized; solid.
- **unfulfilled** — stated/planned but never built → candidate future task.
- **tacit / under-specified** — built but never stated (*the gold*) → must it be
  captured, or was it scope creep nobody asked for?
- **ignore** — incidental, not intent.

If `INTENT.md` already has an entry for this task, reconcile as a **delta** — only
review what changed since the last validated baseline.

### 4. Distill what the user vets → INTENT.md

Append (never overwrite) the user-ratified intent to root `INTENT.md`, under a
`## task NNN · <run-id>` heading with the run marker. Only what the user
explicitly approves goes in — ratification is a human act.

### 5. Feed it forward

- Fold confirmed/tacit corrections into `MIND_MAP.md` (propose; let the user apply).
- Turn unfulfilled intent into candidate tasks (`pb-tasks new …`), with the user's go-ahead.

## Notes

- Blindness is enforced by an evidence-only sandbox per layer, not just by
  instruction — but it's strong, not a formal jail. Treat a report that
  references things outside its layer as suspect.
- Heavyweight by design (4 model calls). Run it occasionally on a finished unit,
  not every task.
