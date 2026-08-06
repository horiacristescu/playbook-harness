# {num:03d} - {title}

> **Gate discipline:** One gate → do work → check box → next gate.
> Never batch. Never backfill. The document IS the execution trace.
> **Closing a gate:** check the box, append your outcome. Never replace the original text.
> Design Phase = orientation (one gate, brief answer). Work Plan = real work (one gate, full effort).
> If you see the same gate 5+ times in the hook echo, you're drifting — STOP and update.

## Status
pending

> **Before filling this in:** run `pb-tasks work <N>` to activate this task. Hooks won't enforce until activated.

## Intent
(what we want to achieve — the outcome, not the activity)

## Why
(why this matters now — urgency, context, what breaks if delayed)

## References
- [ ] Context: `grep -Ein "keyword1|keyword2" MIND_MAP.md` → paste relevant excerpts below
- Origin: Mxxx
- Playbook: {playbook}
- Note: Don't hardcode task numbers in plans — `pb-tasks new` auto-increments.

---

## Design Phase

> **Write a 1-sentence answer for each gate.** A bare checkmark means you skipped it.
> Complete these gates before writing the work plan.
> (The `/playbook` skill has workflow patterns if you need a reference.)

### Understand
- [ ] Restate the request in my own words. What does the user actually want?
- [ ] Critique: Am I solving the stated problem or a different one I find more interesting?
- [ ] What would "done" look like? How will we know the task succeeded?
- [ ] What is OUT of scope for this task?

### Structure
- [ ] What kind of work is this? (build / investigate / evaluate / decide / combination?) If combination, what's the sequence? If >15 gates or uncertain approach, where do you stop and validate direction?

### Reflection Gates
- [ ] Wrote task-specific check questions (Bad: "is this working?" Good: "Does the output include the progress counter?" — the answer should require evidence, not just yes/no)
- [ ] Before the riskiest step: what would make you stop and reconsider?
- [ ] If judging quality before building: is the gap worth closing?

### Verify
- [ ] Review the work plan. If a likely growth point exists, add it to the plan now.
- [ ] Does the work plan include moments where you stop and question your approach — not just execute?
- [ ] Checkpoint: Would a fresh agent understand this task and execute it well?
- [ ] The work plan below has the right granularity (not too coarse, not micro-steps)

## Plan Review
- [ ] Run `pb-tasks plan-review <N>` — wait for it to finish (it edits this file). Re-read this file to see its findings below, then address valid concerns by revising Work Plan gates. **Justify lens:** does every work gate trace up to something in Intent/Design? Are there gates that justify nothing above them (scope creep)? Intent claims with no gate to satisfy them (gaps)?
- [ ] **Triage plan-review findings: judge = opinion, not gospel.** For each finding, document accept (with rationale) / park (with rationale) / reject (with rationale). Push back where you have concrete evidence — you live with the outcomes, the reviewer doesn't. Verify file:line claims before applying — single-judge reviews can cite wrong locations.

(plan review findings appear here)

---

## Work Plan

> For each work section: what could go wrong? How will you know it worked? (specific check, not "looks good")
> Standard feature: 6-8 work gates + tests. Large tasks work fine — if >15 gates, add a mid-point checkpoint to reassess direction.

(write work gates here)

---

## Implementation Review
- [ ] Run `pb-tasks impl-review <N>` — wait for it to finish (it edits this file). Re-read findings. **Satisfy lens:** does every Intent claim trace down through code to tests? Where does the chain break?
- [ ] **Triage impl-review findings: judge = opinion, not gospel.** For each finding, document accept (with rationale) / park (with rationale) / reject (with rationale). Push back where you have concrete evidence — you live with the outcomes, the reviewer doesn't. Verify file:line claims before applying — single-judge reviews can cite wrong locations.

(implementation review findings appear here)

---

## Pre-review
- [ ] All tests pass
- [ ] No debug artifacts
- [ ] MIND_MAP.md updated if new insights emerged

## Parked
(Findings or ideas that emerged during work but are out of scope. Describe each with enough context for a future task to pick it up.)

---

## Standing Orders
- **Expand dynamically**: When you discover something you'll need to do, write new gates immediately — don't wait until you get there.
- **Steer openly**: If your direction changes, edit your open (unchecked) gates to reflect reality. The plan is alive, not a contract.
- **Never defer awareness**: The moment you realize work exists, capture it. Forgetting is the failure mode, not having too many gates.
