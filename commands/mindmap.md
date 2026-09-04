---
description: Generate a mind map by analyzing the current codebase
allowed-tools: [Read, Write, Edit, Bash, Glob, Grep]
---
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/mindmap.md","source_hash":"7c11cf70bf7425542abf58d6da11ea3daaf5e27c2328af16278d53fb838266bc"} -->
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/mindmap.md","source_hash":"8dad647f3dfa4483a13ae6ed1c1d3e0cdfcbb4e80799200154ceed2ee2976c45"} -->
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/mindmap.md","source_hash":"206042a85b5e2545bedd040e167c714f9ebe6dd1e53d18b6a2c4f51a1ba89974"} -->
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/mindmap.md","source_hash":"dd4e769346fe9ab08060549778a84809d320d7b1b0e1865b517fdea2c8d23744"} -->

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
