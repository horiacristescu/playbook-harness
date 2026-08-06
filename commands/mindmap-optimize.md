---
description: Analyze mind map for staleness, compression opportunities, and sync issues
allowed-tools: [Read, Bash, Grep, Glob]
---

# Mind Map Optimize

Analyze `MIND_MAP.md` for staleness, compression opportunities, and overflow sync issues. Report findings — do NOT auto-edit.

## Instructions

Perform every step. Collect findings into a final report.

### 1. Load mind map

Read `MIND_MAP.md` and `MIND_MAP_OVERFLOW.md`. Extract all node IDs and titles from each file.

### 2. Sync check

Run the CLI sync command:

```bash
pb-tasks mindmap-sync
```

This reports: size stats, missing nodes, content drift (full nodes where main and overflow diverge), and broken cross-references.

- **Main ahead**: nodes updated in MIND_MAP.md but not overflow. Fix with `pb-tasks mindmap-sync --fix`.
- **Overflow ahead**: nodes with more detail in overflow than main. These need judgment — read the overflow version and decide if the extra detail belongs in main (promote) or if main's compression is correct (leave it).

If there are main-ahead nodes, run `pb-tasks mindmap-sync --fix` to auto-sync them to overflow.

### 3. Staleness scan

For each node in MIND_MAP.md, check if the files/paths/concepts it references still exist:

```bash
# For each node, extract file paths and key identifiers mentioned, then check if they exist
# Examples: src/tasks/cli.py, .agent/tasks/, hooks.json, etc.
```

Flag nodes that reference files or directories that no longer exist.

### 4. Size and compression analysis

Report:
- MIND_MAP.md size (chars and approximate tokens at 4 chars/token)
- How many nodes are full vs summary (↗) vs title-only (↗ + single line)
- Nodes that are full but could be compressed (long content, low cross-reference count from other nodes)
- Nodes that are ↗ summary but have been heavily edited recently (may need promotion back to full)

### 5. Abandoned task scan

```bash
# List tasks that are pending or in_progress
pb-tasks list --pending
```

Flag any pending/in_progress tasks older than 2 weeks with no recent git activity in their directory.

### 6. Cross-reference health

Check that all `[N]` references within node text point to nodes that actually exist. Flag broken cross-references.

### 7. Report

Print a structured report:

```
## Mind Map Health Report

### Size
- MIND_MAP.md: X chars (~Y tokens)
- MIND_MAP_OVERFLOW.md: X chars
- Nodes: N total (F full, S summary, T title-only)

### Sync Issues
(list or "None")

### Content Drift
(nodes where main and overflow have diverged, with direction, or "None")

### Stale Nodes
(nodes referencing nonexistent files/paths, or "None")

### Broken Cross-References
(list or "None")

### Compression Opportunities
(full nodes that could be demoted, or "None")

### Promotion Candidates
(summary/title-only nodes in active areas, or "None")

### Abandoned Tasks
(list or "None")

### Recommended Actions
1. ...
```

Present the report. Let the user decide what to act on.
