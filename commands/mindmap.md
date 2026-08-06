---
description: Generate a mind map by analyzing the current codebase
allowed-tools: [Read, Write, Edit, Bash, Glob, Grep]
---

Generate a populated `MIND_MAP.md` for this project. Work directly — no Task agents — so the user can steer between steps.

## Process

1. **Scan codebase:** Read READMEs, configs, entry points. Map directories, tech stack, architecture, data flow.
2. **Mine git history:** Commit timeline, development phases, major milestones.
3. **Construct map:** Plan node hierarchy, write nodes, weave links. Use the format from the `mindmap.md` skill.
4. **Write to `MIND_MAP.md`** in the project root. If one exists, replace scaffold content but preserve any existing populated nodes.

## Key Rules

- Each node must be a **single line** (grep-friendly)
- Target **20-50 nodes** with cross-links
- Routing nodes [1-5] link to everything else
- Links embedded naturally in text, not clustered at end
- Include git history with commit hashes
- Reference actual file paths, function names, numbers
