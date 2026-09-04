---
description: Analyze the project mind map for staleness, routing, and compression opportunities
allowed-tools: [Read, Bash, Grep, Glob]
---
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/mindmap-optimize.md","source_hash":"b84678987d1e091921809c902907a3b4e8c113a5e777ec36ccf9f60f0289e523"} -->
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/mindmap-optimize.md","source_hash":"06c8fc348c8d0a97390055be4191fb530c6f8d5e2cc798065a09e85e5348eb58"} -->
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/mindmap-optimize.md","source_hash":"a9cc575bad7dea4445de30572030608e9bafd00eb353dea4270d077e769024f3"} -->
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/mindmap-optimize.md","source_hash":"e4ef940381cf9f4c8118bc5f49d7dbb0413a4e4bdd1a42e39d8da5db13d38595"} -->

# Mind Map Optimize

Analyze the sole authoritative `MIND_MAP.md`. Report findings; do not auto-edit.

## Instructions

1. Read the entire map and extract every node ID and title.
2. Check for duplicate or missing IDs/titles and references to nonexistent nodes.
3. Check whether referenced files, paths, commands, and concepts still exist.
4. Report character count, approximate tokens at four characters per token, node count,
   and average node size. The bootstrap load budget is 25KB.
5. Identify nodes that can be compressed without losing their causal reason or a
   necessary qualification. Route deep evidence to a cited task, document, or source
   path instead of proposing a parallel map.
6. Run `pb-tasks list --pending` and flag pending work older than two weeks with no
   recent activity in its task directory.

Use this report shape:

```text
## Mind Map Health Report

### Size
- MIND_MAP.md: X chars (~Y tokens)
- Nodes: N total

### Structural Issues
(duplicate/missing IDs or titles, or "None")

### Stale Nodes
(nonexistent files/paths or obsolete concepts, or "None")

### Broken Cross-References
(list or "None")

### Compression Opportunities
(nodes whose useful reason survives a shorter form, or "None")

### Evidence-routing Candidates
(detail that belongs behind a citation, or "None")

### Abandoned Tasks
(list or "None")

### Recommended Actions
1. ...
```

Present the report and let the user decide what to act on.
