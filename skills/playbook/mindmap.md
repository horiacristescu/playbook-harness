# Mind Map — Format Reference

A mind map is a **read/write memory** for AI agents — a knowledge graph that speeds up bootstrapping and carries context across sessions. This is not a log. Nodes are living documents that evolve as understanding deepens.

## Node Format

**Each node must be a single line** — no line breaks within a node. This makes grep trivial: `grep "^\[5\]" MIND_MAP.md` returns the entire node.

```
[N] **Title** - Text with [link-nr] embedded naturally. Dense but readable, 150-400 words per node. Every sentence adds information. All on one line, no matter how long.
```

Every `MIND_MAP.md` starts with this header:

```markdown
> **For AI Agents:** This mind map is your primary knowledge index. Read overview nodes [1-5] first, then follow links [N] to find what you need. Always reference node IDs. When you make changes, update outdated nodes immediately — especially overview nodes. Add new nodes only for genuinely new concepts. Keep it compact (20-50 nodes typical). The mind map wraps every task: consult it, rely on it, update it.
```

## Routing Nodes [1-5]

The first 5 nodes are **routing hubs** — they point to everything else and get updated frequently:

- [1] **Overview** — what this is, main entry point, links to all major concepts
- [2-5] **Major themes** — each covers a domain, links to relevant detail nodes

**Routing nodes change as the map grows.** When you add node [47] about authentication, go back and add `[47]` to the relevant routing node.

## Node Hierarchy

| Level | Nodes | Content |
|-------|-------|---------|
| Foundation | 1-5 | Overview, core theory, data flow, major architectural layers |
| Systems | 6-15 | Major subsystems, key features, components |
| Implementation | 16-20 | Tech stack, history, workflow, roadmap, principles |
| Deep dives | 21+ | Algorithms, optimization, error handling, performance |

**Scale:** Simple topics 15-30 nodes. Standard projects 30-60. Complex systems 60-150. Large codebases 150+ (routing nodes become critical).

## Link Principles

Links `[N]` flow naturally in sentences, not listed at the end:
- Good: "The storage layer [6] handles persistence"
- Bad: "The storage layer handles persistence [6][7][8][12]"

Every node links to 2-3+ others; important nodes link to 5-10. Bidirectional: if [5] mentions [12], [12] should mention [5]. Link every 1-2 sentences, not every clause.

**Density targets:**
- Overview nodes: 5-10 links
- System nodes: 3-7 links
- Implementation nodes: 2-5 links
- Specialized nodes: 2-4 links

## Writing Style

- Use specific names: "The PCAVisualizer class in pcaVisualizer.ts" not "the visualizer"
- Include numbers: "5-10 features", "23,000+ lines"
- Reference actual filenames, function names, variable names
- Explain WHY decisions were made, not just WHAT exists
- When information changes, update existing nodes — don't create duplicates

## Examples

### Good — Single-Line Nodes, Natural Linking

```
[1] **Project Overview** - TaskFlow is a command-line task manager built in Python that emphasizes keyboard-driven workflows. The core architecture separates task storage [6] from the rendering layer [7], allowing multiple output formats without touching business logic. Users interact primarily through vim-style keybindings [12] which were chosen over menus for speed. The data model [3] uses a flat JSON structure rather than nested categories because real-world task hierarchies rarely go deeper than two levels. Integration with external calendars [9] was added in v2.0 after user feedback showed most people needed deadline sync.

[6] **Storage Layer** - The StorageManager class in storage.py handles all persistence operations [1][3]. It maintains an in-memory cache that syncs to disk on every write, which is acceptable for typical task volumes (under 10,000 items). The JSON file format was chosen over SQLite for portability and easy manual editing. Backup rotation keeps the last 5 versions automatically.
```

### Bad — Links Clustered at End

```
[1] **Project Overview** - TaskFlow is a command-line task manager... Integration was added in v2.0. [3][6][7][9][12]
```

### Bad — Over-Linked

```
[1] **Project Overview** - TaskFlow [2] is a command-line [4] task manager [3] built in Python [5] that emphasizes keyboard-driven [12] workflows [7].
```

## Node Templates

**System node:**
```
[N] **System Name** - Brief definition and purpose [parent-node]. The system consists of COMPONENT1 which handles TASK1 [detail-node], COMPONENT2 for TASK2 [detail-node]. Implementation resides in path/to/files using TECHNOLOGY [tech-node]. Integrates with EXTERNAL_SYSTEM [integration-node] through API_METHOD. Key parameters include PARAM1 (range, default) and PARAM2 (type, purpose) [parameter-node].
```

**History node:**
```
[N] **Development History** - Evolved over TIMESPAN from DATE1 to DATE2 [overview-node]. Commit HASH1 (DATE) created initial structure with FRAMEWORK [theory-node]. Commit HASH2 (DATE) was the major milestone: N insertions creating SYSTEM1 [node], SYSTEM2 [node]. Commit HASH3 (DATE) introduced FEATURE [feature-node]. Total: N commits, ~N lines, transforming from STATE1 to STATE2 [principle-node].
```

## Grep-Friendly

```bash
grep "^\[5\]" MIND_MAP.md         # Returns entire node 5 (one line)
grep "\[5\]" MIND_MAP.md          # All lines referencing node 5
grep "authentication" MIND_MAP.md # Find by keyword
```

## Quality Checklist

- All major systems/features have nodes
- Every significant file/component is mentioned
- Development history captured with commit hashes
- Every node has 2+ links, important nodes 5+
- Links embedded naturally in text, bidirectional where appropriate
- Each node has a clear title, first sentence defines the concept
- Actual file/function names used, not generic descriptions
- Each node is a single line (grep-friendly)
