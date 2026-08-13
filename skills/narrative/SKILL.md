---
name: narrative
description: Read a project's chat log as a narrated timeline — arcs of intent over spans of attention over raw comments — and render it as a page the user can scan to see what happened over hours or days. Use when asked for /narrative, a narrative or overview of recent work, "what happened while I was away", "what have we been doing", a readable timeline of a session or a day, or when extending an existing narrative with newer comments. Do not use for selecting what deserves inheritance (that is /culture), for reviewing how one delivery went (that is a task retrospective), or for extracting a single task's messages (that is `pb-tasks context`).
---

# Narrative

Turn `.agent/chat_log.md` into something a person can read at a glance.

The log is a flat stream of human messages, gate closures and lifecycle events.
Narrative gives it three layers: **arcs** of intent, containing **spans** of
attention, containing the raw **comments**. You author the arcs and spans by
reading; `pb-tasks narrative` parses, validates and renders.

The reader is the user, catching up over hours — not an agent loading context.
Write for someone who was away and wants to know what happened.

## The division of labour

`pb-tasks narrative` will never invent a boundary. Segmentation is judgment, so
it stays with you. The CLI parses the log, checks what you wrote, renders the
page, and tells you what does not add up.

```bash
pb-tasks narrative                 # what is narrated, what is new
pb-tasks narrative --pending       # the un-narrated comments, one line each
pb-tasks narrative --render        # write .agent/narrative/narrative.html
```

No task is required. This is read-only observability — CLAUDE.md exempts it.
Activate a task only if you are changing the tool itself.

## Procedure

1. **Check what is already narrated.** Run `pb-tasks narrative`. It reports how
   many comments are annotated and how many are new.
2. **Extend; do not restart.** If `annotations.json` already has arcs, read
   *only* the pending region and add to it. Re-narrating history that is already
   narrated is the main way this task is done badly — it burns effort and
   discards judgment someone already exercised.
3. **Read the pending comments at low resolution first.** `--pending` gives one
   line each. Scan the whole set before opening anything. Open full text only
   where the narrative actually needs it — the `nub` skill covers this
   progressive pattern if the region is large.
4. **Author spans, then group them into arcs.** Edit
   `.agent/narrative/annotations.json` directly; it is ordinary JSON meant to be
   hand-edited.
5. **Render and check.** `--render` reports any comment inside the narrated
   range that belongs to no span. Cover it or adjust a boundary.

## What the layers mean

A **span** is one continuous stretch of attention: what was being done here.
Bounded by `(id, ts)` pairs, contiguous, exhaustive within the narrated range.

An **arc** is a thread of intent across spans, usually across hours or days,
and it has an **outcome** — what it produced. This is the test of whether the
arc layer earns its place: if an arc's description would work equally well as a
span's, you have not found an arc, only a bigger span.

Both titles carry an object prefix naming what the work was *about*:

```
[tmux]      The session system meets its user
[rubrics]   Inventing rubric tests for the harness's prose
[chat log]  Observing the observer
```

Keep objects short and reuse them across spans — repetition is signal, showing a
thread recurring across the timeline.

## Standards for the writing

- **Quote the user.** A span about frustration should contain the actual words.
  Real quotes carry more than any paraphrase.
- **Say what happened, not that something happened.** "Three remediation gates
  landed" tells the reader nothing. Name the repairs.
- **Do not re-summarize gate text.** A gate closure is already a distillation;
  summarizing it again loses the evidence that made it worth writing. Describe
  what the gate *was for* and let its text stand.
- **Length is free.** The page is for a human, not a context window. Descriptions
  can be several sentences where the work deserves it.
- **Note causal links between arcs.** The most useful thing a narrative does is
  show that a frustration on one evening became a repair the next morning.

## Reading your own output

Two signals tell you the annotation is straining:

- **Object mismatch.** An arc whose spans carry unrelated objects is holding
  work that belongs to a different thread. Arcs are contiguous by construction,
  so some mismatch is expected — persistent mismatch means the boundary is wrong.
- **A span you cannot describe without "and".** That is two spans.

## Do not use this skill for

- **`/culture`** — selecting what deserves inheritance. Culture judges and
  discards; narrative preserves everything and judges nothing.
- **A task retrospective** — how one delivery went, scoped to a task.
- **`pb-tasks context <N>`** — extracting one task's messages.

## Cautions

The chat log is provenance with boundaries (MIND_MAP [11]): historical bodies
can be blank or duplicated, message counters reset, and captured terminal text
can contain private details. Do not treat the page as safe to publish, and cite
timestamps alongside ids when referring to old entries.
