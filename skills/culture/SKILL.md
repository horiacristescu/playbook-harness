---
name: culture
description: Create and run an open-ended cultural retrospective task that reads a project's chat log, historical task gates and review artifacts, and mind map to select what future participants should inherit. Use when asked for /culture, project culture, cultural inheritance, continuity across agents or tools, valuable context from the project lifespan, or a compact account of ideas, corrections, practices, relationships, failures, and open questions worth transmitting. Do not use for an ordinary delivery retrospective, project summary, style guide, policy extraction, or a test of predefined cultural categories.
---

# Culture

Treat a cultural retrospective as project-scale temporal attention. Inspect the
project's lived history and decide which context is likely to remain valuable,
in which future situations, and whether a compact inheritance can prevent more
cost than it imposes. Let cultural kinds emerge from the record; do not begin
with a values taxonomy, rubric, or expected conclusions.

## Start the task

1. Run the project's bootstrap command if it has not already run.
2. Inspect recent tasks. If an unfinished cultural-retrospective task exists,
   reopen it and append any missing gates from the bundled template.
3. Otherwise run:

   ```bash
   pb-tasks new quick cultural-retrospective "Select, with evidence, what from the project's lived history is worth transmitting to future participants, why, and in which contexts it will be useful"
   ```

   Use the project's local tasks command if `pb-tasks` is unavailable.
4. Locate the new task file from the CLI output. Do not create a task directory
   manually.
5. Before activation, adapt [`assets/task-template.md`](assets/task-template.md)
   into the new `task.md`. Replace `{{NNN}}`, `{{TITLE}}`, and bracketed
   placeholders. Preserve `pending` status and every unchecked gate.
6. Activate it with `pb-tasks work <N>` (or the project's local tasks command),
   read the task printed by the CLI, and execute one gate at a time.

## Interpret the program

- Keep the primary question open: what deserves inheritance, and what future
  cost or possibility does preserving it affect?
- Read the complete primary history at progressive resolution. Search is an
  omission tool, not a replacement for chronological coverage.
- Treat `chat_log.md`, historical `task.md` gates and annotations, judge/review
  artifacts, `MIND_MAP.md`, and its overflow as distinct evidence surfaces.
- Record candidates before organizing them. Promote items through downstream
  influence, costly correction, repeated rediscovery, generative compression,
  changed practice or belief, or productive unresolved tension—not eloquence or
  frequency alone.
- For every candidate, record where it may matter later and the cheapest useful
  transmission form: a compact phrase, lineage, conditional routing note,
  warning, practice, question, or pointer to fuller evidence.
- Preserve important supersession as `earlier claim → pressure → current claim`.
  Keep authorship exact; never turn an agent inference into a user's position.
- Use independent readers only for omission or skeptical passes. They do not
  replace the accountable editor's end-to-end contact with the primary history,
  and their lenses are not a voting panel.
- Keep raw sources immutable. Put ledgers, coverage notes, and review artifacts
  in the task directory; write the compact cultural artifact at the output path
  chosen in the task.
- Do not automatically inject the full artifact into every future session.
  Design a routing path so successors load an inheritance when its context is
  relevant.

## Completion rule

Do not stop at an attractive item count. Complete only after every primary
history segment has been inspected, pivotal candidates have been reread in
context, an independent omission and supersession pass has run, citations and
privacy have been checked, and two consecutive review passes add no new
cultural kind or materially change why an item should be preserved.
