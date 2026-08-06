<!-- PLAYBOOK TEMPLATE
  This file was installed by the playbook plugin. It contains the universal
  workflow instructions that every playbook-managed project needs.

  IF THIS FILE IS AT `.claude/templates/CLAUDE.md`:
    Merge the sections below into the project's root CLAUDE.md.
    Preserve all existing project-specific content (project description,
    custom rules, architecture notes). Add or update the sections from
    this template. If those sections already exist in CLAUDE.md, update
    them to match this version without duplicating content.

  IF THIS FILE IS AT `CLAUDE.md` (project root):
    Replace "Project Name" below with the actual project name.
    Add any project-specific instructions after the template sections.
-->

# Project Name

## Start Here

```bash
pb-tasks bootstrap                   # loads mind map, skills, pending tasks
```

Then **ask the user** what they want to work on. Don't autonomously pick a task.

## Mind Map

Read `MIND_MAP.md` at project root before starting any work. It's the project's institutional memory — architecture, decisions, history, and reasoning. Consult it before each task, update it after completing one.

## Task Lifecycle

These are **your** commands to run — the user never calls the tasks CLI.

Start or activate a task **only for repository code work**. A task is not
required for plans or Markdown documentation, read-only review, operational
shell work that does not edit repository code, or starting/stopping services.
If operational work leads to a code change, activate a task before that edit.

Before creating a task, inspect the most recent 2–3 tasks and cluster matching
work into the related task: reopen it with `pb-tasks work <N>` and append unchecked gates.
Create a new task only when none of those recent tasks honestly matches.

```
pb-tasks new <type> <name> [intent]   # creates task.md — then immediately:
pb-tasks work <N>                     # activate it — hooks start enforcing
```

Work through task.md gates: Design Phase (understand, structure, verify) → Work Plan (build/investigate gates) → Pre-review. Check each gate's checkbox as you complete it, appending your observations on the same line.

When done: `pb-tasks work done` — deactivates the task, sets status to done.

The task.md **is** the execution trace. Never skip checkboxes. Never backfill. One gate at a time.

## CLI

```bash
pb-tasks work <number>               # activate task, hook starts tracking
pb-tasks work done                   # deactivate when finished
pb-tasks new <type> <name>           # create task — does NOT activate
pb-tasks list [--pending]            # task overview
pb-tasks status                      # current gate position
pb-tasks bootstrap                   # orientation: mind map + skills + pending
```

## Don't

- Create task directories manually — always `pb-tasks new`. For matching follow-up work, reopen one of the most recent 2–3 tasks with `pb-tasks work <N>` and append unchecked gates.
- Edit `.agent/sessions/` state files directly — use `pb-tasks work <N>` / `pb-tasks work done`
- Edit `## Status` in task.md directly — use `pb-tasks work done`
- Skip task.md checkboxes — they're your observable progress
- Start coding without an active task — blocked by hook until `pb-tasks work <N>`
