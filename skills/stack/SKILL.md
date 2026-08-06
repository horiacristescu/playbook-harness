---
name: stack
description: >
  Default tech-stack picks for starting a new project (the "Bedrock stack").
  A shopping list of good defaults — boring, typed, observable — for Python,
  TypeScript, and observability. Use when scaffolding a fresh repo or choosing
  tools and the user hasn't stated a preference.
---

# Stack

A shopping list of default picks, not a mandate. Reach for these on a **new
project where the user hasn't stated a preference**. If the user names a
different tool, build that well instead.

Picks optimize three axes at once:
1. **Known to LLMs** — highest-training-data option, so the model writes correct idioms.
2. **Loud failure** — typed, autofixable, located errors the agent can recover from.
3. **Observable** — you can see what the work does (a test, a log, a UI, `--json`).

Domain lists (load the one you need):
- [`python.md`](python.md)
- [`typescript.md`](typescript.md)
- [`observability.md`](observability.md)

Post-cutoff pins go in `AGENTS.md` (each domain file marks them ⚑) — the model
knows the wrong version otherwise.
