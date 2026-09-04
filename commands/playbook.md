---
description: Show workflow patterns and task execution guidance
argument-hint: "[pattern-name]"
allowed-tools: [Read, Glob, Grep]
---
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/playbook.md","source_hash":"ab12a66873e5106df2f31b134ad08c72bdd413aec10e851ced19e4dd41222fb9"} -->
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/playbook.md","source_hash":"dd430e7dd2252216e5529b7e8dcdcc58ad11e2e366ebf2a2f6f29f8775043126"} -->
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/playbook.md","source_hash":"17245f5712743152fcc97ca0901b335c3591b76fd2322b31fb25c72a97ab40e8"} -->
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/playbook.md","source_hash":"049a3126bd973ff3b9b860b55f8cb0df9d5eeb02613d9339972c80fc697dd3f4"} -->

# Playbook

Show workflow patterns for task execution. Five core patterns: Build,
Investigate, Evaluate, Decide, UI Debug.

**User asked for:** $ARGUMENTS

## Instructions

First, check if this project has been initialized: look for `CLAUDE.md` at the project root AND `.agent/tasks/` directory. If either is missing, tell the user:

> This project hasn't been initialized yet. Run `/init` to set up the playbook workflow (creates CLAUDE.md, MIND_MAP.md, and the task CLI).

Then stop - don't show patterns for an uninitialized project.

If the project IS initialized, read the full playbook skill from skills/playbook/SKILL.md.

If the user specified a pattern name, show that pattern's details.
If no argument, show the pattern overview and ask which one they need.

Available patterns:
- **Build** — step-test interleave for implementing features
- **Investigate** — observe-hypothesize-test for debugging/research
- **Evaluate** — pre-check, lenses, verdict for reviewing quality
- **Decide** — options, comparison, commitment for architecture choices
- **UI Debug** — script-probe-screenshot for browser bugs

Also explain: reflection gates (Critique, Checkpoint, Replan), the Design Phase,
and how to compose patterns for complex tasks.
