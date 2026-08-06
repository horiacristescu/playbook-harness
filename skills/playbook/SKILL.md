---
name: playbook
description: >
  Composable workflow patterns for task execution. Four core patterns (Build,
  Fix, Investigate, Evaluate) plus UI Debug and structural reflection gates.
  Pick what fits, compose as needed. The rhythm matters more than the content.
argument-hint: [pattern-name]
---

# Playbook

## Rhythm

Every task follows a rhythm: **push → stop → push → stop → close**.

Pushing is doing the work. Stopping is questioning the work. Tasks that are
all push produce shallow output. Tasks that alternate push and stop produce
depth. Mature adoption shows the full pattern: Design Phase as foundation,
quantified checkpoints, critique at every transition. The reflection gates
below enforce the stops.

### Reflection Gates

Insert these between work sections. They are not optional. Their purpose
is mode-switching: forcing the shift from collecting to interpreting. Without
them, the agent stays in action mode and produces shallow output.

**Critique** (use FIRST — before diving in, and when output feels thin):
```
- [ ] Critique: What's shallow here? What am I avoiding? What would a skeptic ask?
```
Critique was the highest-leverage gate in the first task that used it. Starting
with "what's wrong with the previous approach?" shaped everything that followed.
Use it at task start, not just when struggling.

**Checkpoint** (after completing a work section):
```
- [ ] Checkpoint: What did I just learn that I didn't expect? Is the approach working? Scope check: has scope changed from the original Intent? Continue, replan, or split?
```
The question "what didn't I expect?" is more generative than "is this working?"
It forces noticing, not just confirming. **Investigation checkpoints must include
a concrete example** — "Given input X, parse returns Y with structure Z" — not
just "I understand the interface." Run-007 said "I understand parse()" then got
the data model wrong. A concrete example would have caught it.

**Replan** (when reality diverges from the plan):
```
- [ ] Replan: Original assumption was X. Reality is Y. New approach:
```

Use Critique at the start and at transitions. Use Checkpoint after every
pattern section. Use Replan when stuck or when critique reveals a gap.

### Emergent Gate Types

- **Parked:** Explicit "not doing this" with enough context for a future task to pick it up. Prevents scope creep while preserving work.
- **Extension:** Task spawns new scope within the document, with Critique at the boundary. Scope grows explicitly, not silently.
- **Adversarial examples:** "Who was right?" with quoted evidence. Tests the model against reality, not just coverage metrics.
- **Quantified checkpoints:** Numbers, not yes/no. "57 tests doing real work" beats "tests look good."
- **Decide:** Inline tradeoff gate: `- [ ] Decide: A is ___. B is ___. Choosing ___ because ___.` For major decisions, add an options table with pros/cons/cost-to-undo before deciding.

---

## Design Phase

The single biggest quality predictor across 14 analyzed tasks. Tasks with
thorough Design Phases produced better work than tasks that rushed them —
regardless of pattern choice or task complexity.

Design Phase lives in the base template. The playbook's job is to explain why
it matters and how to do it well:

- **Write 1-sentence answers**, not bare checkmarks. A checked box with no
  annotation means you skipped the thinking.
- **Define OUT of scope** before writing gates. Scope creep starts when
  boundaries aren't explicit.
- **Write task-specific checkpoint questions at authoring time.** Bad: "is this
  working?" Good: "Does the output include the progress counter?" The quality
  of checkpoints is determined at task creation, not during execution.
- **If >15 gates or uncertain approach:** add a mid-point checkpoint to reassess
  direction. Large tasks work fine with hook enforcement — the checkpoint is for
  catching misframing early, not a gate count limit.

The Design Phase is orientation, not ceremony. Complete it quickly but honestly.
The goal is to catch misframing (picking the wrong pattern), overscoping
(planning for 82 files when 10 suffice), and missing reflection gates before
work begins.

---

## Patterns

### Build

Step-test interleave. For features, refactoring, infra, docs, ports.

**Ask before planning:**
1. What does "done" look like — concretely, not vaguely? *(tasks with specific acceptance criteria executed cleanly; vague "done" led to scope creep)*
2. After each step, what's the smallest test that proves it worked? *(deferred tests = quality drops — never let a step pass without paired verification)*
   - **Property tests** (`hypothesis`): Consider for data transformations, parsers, and arithmetic — when you can state an invariant beyond "it doesn't crash." Generate inputs that match the function's actual domain, not the widest possible type. Otherwise, example tests are enough.
3. What are you assuming about the existing code that you haven't verified? *(the plan is a debugging surface for invisible assumptions — wrong beliefs about architecture, APIs, or data flow cause cascading rework when they surface late)*
4. Are you building or refactoring? Don't mix them in the same step. *(refactors that become features are the #1 scope explosion pattern)*
5. Does this touch existing contracts other code depends on? *(judge catches wiring gaps when >2 files are touched — consider running it)*

```
- [ ] Step: [what to implement]
- [ ] Test: [what to verify]
- [ ] Checkpoint: [scope still matching Intent?]
```

Right-sizing: UX polish → 1-3 gates. Cleanup/bugfix → 2-4. Feature → 6-8. Port → 8-12. Research → 10-15. 20+ → validate approach first.

---

### Fix

Locate, correct, verify. For bugfixes and cleanup.

**Ask before planning:**
1. Do you actually know the root cause, or are you assuming? *(the "assumed root cause" trap: one task assumed 3 causes, only 1 was real — investigate first if uncertain)*
2. What's the smallest change that fixes it? *(best fixes were 1-3 gates; full Design Phase is overhead for known-cause bugs)*
3. What will you grep/test to confirm it's fixed? *(for cleanup: `grep -r 'removed_thing'` returning zero is the universal test)*
4. What adjacent code might break from this change? *(adjacent discovery is the norm — expect to find related issues)*
5. Did you find something else while fixing? Capture it, don't expand scope. *(cleanup chains from discovery — keep scope tight, park the rest)*

```
- [ ] Fix: [what to change]
- [ ] Verify: [grep/test that confirms]
```

For unknown-cause bugs, compose Investigate → Fix. For cleanup, grep-verify-delete.

---

### Investigate

Hypothesize-test-conclude in rounds. For research, debugging, exploration.

**Ask before planning:**
1. What's your hypothesis before you start looking? *(state it first — no fishing; the hypothesis shapes what you notice)*
2. Are you still finding new information, or confirming what you already believe? *(the critique round that asks "am I confirming or testing?" was the highest-leverage gate in research tasks)*
3. What would change your mind? *(research without convergence forcing becomes eternal — if 2 rounds produce no new position, stop)*
4. Can you show the evidence, not just describe the conclusion? *(hypothesis/finding tables and captured artifacts were the most useful deliverables)*
5. What emerged that you didn't expect? *(this question is more generative than "is this working?" — it forces noticing)*

```
### Round N: [focus]
- **Hypothesis:** [before testing]
- **Test:** [what to check]
- **Result:** [what happened]
- [ ] Checkpoint: converging or scattering?
```

For docs-from-sources: capture source material before writing. For compression: define retention criteria + findability queries as acceptance tests.

---

### Evaluate

Scan, assess, synthesize. For audits, reviews, evaluation campaigns.

**Ask before planning:**
1. What are you measuring, and are you applying the same lenses to everything? *(consistent lenses enabled cross-item comparison; without them, eval is noise)*
2. Are you assessing or already fixing? Keep them separate. *(audits that mixed assessment with fixing lost objectivity — assess first, fix in a separate phase)*
3. Are the gaps you found material, or cosmetic? *(the sufficiency gate between assessment and action is the key decision — Evaluate always finds gaps, the question is whether they matter)*
4. What are you assuming is NOT broken? *(audits that only checked known-risk areas missed adjacent failures — state your assumptions about what's healthy so they can be tested)*
5. If evaluating N items — have you checked for patterns at the midpoint? *(midpoint checkpoints caught "abort early" signals half the campaigns missed)*

```
### Lenses
| Lens | Assessment | Issues |
|------|------------|--------|

### Verdict
- **Decision:** [PASS/FAIL]
- **If gaps found:** cosmetic or material?
```

For evaluation campaigns: scaffold per-item blocks, require midpoint checkpoint, require synthesis table.

---

### UI Debug

Script-probe-screenshot-diagnose-fix. For UI bugs that don't resolve on first try.

**When to use:** A UI fix looks correct in source but doesn't work in the browser.
Stop reading code. Write a Playwright test that observes the actual DOM.

```markdown
## UI Debug: [what's broken]

- [ ] Prefetch API — is server data correct?
- [ ] Script app to broken state (navigate, click, wait for render)
- [ ] Probe DOM: `$$eval` for className, computed styles, `data-*` attributes
- [ ] Screenshot to `/tmp/debug-<name>.png`
- [ ] Diagnose: write root cause before fixing
- [ ] Fix, re-run probe, convert debug test to regression test
```

**Disciplines:**
- DOM is truth, source code is theory. One `$$eval` beats ten minutes of code reading.
- Add `data-*` attributes to expose render-time values — read them back in the test.
- Prefetch the API first. Half of "frontend bugs" are actually wrong server data.
- Screenshot + DOM probe cover both layers (CSS and structure). Always do both.
- Convert debug test to regression test before closing.

Starter template, techniques, and reference example: [`ui-debug.md`](ui-debug.md)

---

## Composing Patterns

Tasks rarely fit one pattern cleanly. Compose:

- **Feature:** Build. If multiple approaches, add a Decide gate first.
- **Bug fix:** Fix if cause is known. Investigate → Fix if not.
- **Cleanup:** Fix (grep-verify-delete).
- **Refactor:** Evaluate (current state) → Build → Evaluate (same behavior?)
- **Research:** Investigate (rounds) → Decide (what to do with findings)
- **Audit:** Evaluate (scan) → sufficiency gate → Fix (if gaps are material)
- **Eval campaign:** Evaluate (per-input loop + midpoint checkpoint) → synthesis
- **Port:** Investigate (inventory differences) → Build (copy+fix)
- **Spike:** Build (prototype) → Evaluate (feasible?)
- **Incident:** Investigate (triage) → Fix → Evaluate (no regression)
- **UI bug:** Build (attempt) → UI Debug (if it fails) → Fix (targeted)

Insert **Checkpoint** gates between pattern transitions. Insert **Critique** when shifting from investigation to implementation (the highest-risk transition — premature closure).

**Evaluate → Build/Fix needs a sufficiency gate.** Before acting on gaps found during Evaluate, ask: "Is the gap worth closing, or is the current state sufficient?" Evaluate will always find gaps. The question is whether they matter enough to justify the cost of closing them. Add this gate between Verdict and Build: `- [ ] Sufficiency: gaps found are cosmetic / gaps materially affect correctness — action justified?`

**When redoing work:** Start with Critique of the previous attempt. One redo
started by critiquing the prior attempt's shallow findings — that single gate
shaped the entire second pass. Without it, the redo would have been the same
as the first attempt, just slower.

**Customize checkpoint text.** Generic "is this working?" produces generic answers.
Write task-specific checkpoint questions at task creation time (e.g., "what did
depth reveal that sampling missed?" not just "approach working?").

---

## Type Calibration

Before writing your Work Plan, ask:

1. **Is the template heavier than the task?** Cleanup, bugfix (known cause),
   UX polish, and small ops tasks don't need full Design Phase — just restate
   what to do and the done condition. Match ceremony to weight.

2. **What actually proves it worked?** Not everything is "all tests pass."
   Cleanup → `grep -r` returns zero. UX polish → visual check or screenshot.
   Ops → smoke test in the real environment. Eval campaign → synthesis table.
   Port → grep for old environment references returns zero.

3. **Is there a hidden phase the template doesn't show?** Ports need
   investigate-before-copy. Doc extraction needs source capture before writing.
   Audits need assess-then-decide-then-act. Unknown-cause bugs need
   Investigate before Fix.

4. **What could this break that isn't covered by my test gates?** Refactors →
   do old tests still pass? Ops → does existing workflow still work? Format
   changes → have all callers been updated?

5. **Does judge earn its cost here?** High-value: >2 files touched, public
   contracts (APIs, formats, CLI), eval campaign design, doc rewrites.
   Low-value: ≤2 files, cosmetic changes, known-cause bugfixes.

---

## Commit Gate

After all work patterns complete, before code becomes permanent:

```markdown
### Commit
- [ ] Evidence trail: each pattern section filled, not skipped
- [ ] Tests passing now (re-run, don't trust memory)
- [ ] No debug artifacts, temp files, or accidental changes
- [ ] Diff contains only intended changes
- [ ] Mind map updated with what changed and what was learned
```

---

## Anti-Patterns (from real experience)

- **All push, no stop.** One task cleared 8 gates in sequence with no replanning — produced confidently wrong findings. The same task redone with reflection gates replanned once and caught 3 inflated claims. This is the default mode; it takes ~10-15 tasks to outgrow.
- **Coding during discussion.** If the task is in Decide or Investigate, don't write production code. The agent's default is to start coding when a problem is described.
- **Churning on failure.** If tests fail twice on the same approach, Replan — don't loop. The agent's default is to retry harder, not rethink.
- **Skipping the critique.** The first critique gate ever used was the single highest-leverage gate in that task. It turned a redo into a targeted investigation. The gate that feels unnecessary is the one that matters most.
- **Premature closure.** Moving from Investigate to Build before the conclusion is solid. This is the highest-risk transition in any task.
- **Pre-imposed categories.** One task imposed evaluative buckets before scanning; data was fitted to headings. Let categories emerge from the data instead.
- **Action bias after Evaluate.** Evaluate always finds gaps — you can always find untested code, unhandled edge cases, missing docs. The question isn't "are there gaps?" but "is the gap worth closing?" The Evaluate → Build composition has a ratchet toward action. The missing gate: "Is the current state sufficient for the stated intent, or does the gap materially affect safety/correctness?" If the verdict is PARTIAL PASS and the gaps are edge cases that don't happen in practice, the right action is STOP, not BUILD.
- **Over-planning without validation.** One task planned 22 gates; the right answer was 2 lines of code. If you're writing 15+ gates without a Decide gate first, you're planning in the dark. Validate the approach before committing to a large plan.
- **Working without a task.** Gates are invisible without a task file — this is the highest-leverage failure because everything downstream (reflection, checkpoints, gate echo) depends on a task existing. Create or activate a task before doing work.
- **Overscoping.** The agent defaults to maximal scope; the user must constrain. Start with the smallest sample that answers the question. One analysis scoped from 82 files to 10 — and 10 was enough.
- **Assumed root cause.** Jumping to a fix without confirming the cause. If it's not obvious, compose Investigate → Fix.
- **Full ceremony for trivial changes.** 4 Design Phase gates for 5-line changes. Match template weight to task weight.

---

## Mind Map

Format reference and generation guide for project mind maps (`MIND_MAP.md`).

- **Format:** Node structure, hierarchy, link principles, templates — [`mindmap.md`](mindmap.md)
- **Generation:** Codebase analysis → populated mind map in three phases — [`mindmap-gen.md`](mindmap-gen.md)

---

> Evidence base: patterns and anti-patterns derived from 322 tasks across 14 projects. Details: task 056 (task-type-taxonomy) behavioral observations.
