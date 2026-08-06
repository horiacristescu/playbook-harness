---
description: Enter freehand mode — user drives, agent executes, no gate pressure
allowed-tools: [Read, Write, Edit, Bash, Glob, Grep]
---

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
