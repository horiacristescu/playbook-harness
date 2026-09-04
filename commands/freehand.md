---
description: Enter freehand mode — user drives, agent executes, no gate pressure
allowed-tools: [Read, Write, Edit, Bash, Glob, Grep]
---
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/freehand.md","source_hash":"fedb9dfd5d67c92495a9de0fd2fe295ce8120733ed0ee6635fe37465ece63db4"} -->
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/freehand.md","source_hash":"899e3ce73b3e13bb27e0010231b729dfe52d72f5682b0d5aa314841effd9006a"} -->
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/freehand.md","source_hash":"0c2e4c40b8c87939f51e5d0166988ef46d54e4de812dc0b82edafda880b1abad"} -->
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/freehand.md","source_hash":"f5e61ac919de2ffd3e2891a53d6224d8ecb0295a37a421b8620da228d5930477"} -->

# Freehand

Enter freehand mode for user-directed work without gate pressure.

## Instructions

Run:

```bash
pb-tasks freehand
```

This will either:
- **If a task is active:** insert a Freehand block before the next unchecked gate
- **If no task is active:** create and activate a new task, then insert the Freehand block

Once in freehand mode:
- Wait for user instructions — don't work autonomously
- Drift counter and stop-hook blocking are suppressed
- When the user says done: check `[x] Freehand — <summary>`, run `pb-tasks freehand log`, retro-add checked gates for work done
