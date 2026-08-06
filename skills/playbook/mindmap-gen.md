# Mind Map — Generation from Codebase

How to analyze a code repository and produce a populated `MIND_MAP.md`.
Three phases: scan the codebase, mine git history, construct the map.
See [mindmap.md](mindmap.md) for node format and quality criteria.

---

## Phase 1: Current State Analysis

### 1.1 Reconnaissance

Explore project structure broadly:

- Read README, docs, configuration files (package.json, pyproject.toml, Cargo.toml, etc.)
- Map top-level directories — what lives where
- Identify entry points (main, index, app)
- Note the technology stack from dependencies

### 1.2 Architecture Discovery

Trace the system's shape:

- **Components:** What are the major subsystems? How are they organized?
- **Data flow:** How does information move from input to output?
- **State:** Where is state managed? How do components communicate?
- **Boundaries:** Where does the system talk to the outside world? (APIs, DB, filesystem, network)

Read the critical files completely — don't skim. Core algorithms, state management,
API integration, data pipelines.

### 1.3 Feature Mapping

For each major feature:

- **What** it does
- **How** it's implemented (files, classes, functions)
- **Why** the design was chosen (look for comments, ADRs, commit messages)
- **Dependencies** — what it connects to

### 1.4 Implementation Details

Dive into specifics:

- Key algorithms and patterns
- Configuration and parameters
- Error handling strategies
- TODOs, FIXMEs, known issues

---

## Phase 2: Git History

### 2.1 Timeline

```bash
# Full commit history with stats
git log --all --date=short --stat --pretty=format:"%n=== %h | %ad | %s ===" -- .

# First commit
git log --all --diff-filter=A --date=short --pretty=format:"%h | %ad | %s" -- . | tail -5

# Largest commits (most files changed)
git log --all --shortstat --oneline -- . | head -40
```

### 2.2 Development Phases

Identify from the timeline:

- **Initial creation** — what was the starting point?
- **Major refactors** — large insertions/deletions, file renames
- **Feature additions** — new files, new directories
- **Architecture shifts** — library changes, structural reorganization
- **Stabilization** — bug fixes, refinements, documentation

### 2.3 Commit Details

For each significant commit:

```bash
git show <hash> --stat    # what changed
git show <hash>           # how it changed
```

Record: hash, date, what it did, files changed, which feature/system it relates to.

---

## Phase 3: Construction

### 3.1 Plan Nodes

Before writing, outline the hierarchy:

1. **Nodes 1-5 (Foundation):** Project overview, core concept/theory, data flow, major architectural layers
2. **Nodes 6-15 (Systems):** One node per major subsystem or feature
3. **Nodes 16-20 (Implementation):** Tech stack, development history, workflow, design principles
4. **Nodes 21+ (Deep dives):** Specific algorithms, performance, specialized topics

Target: 20-50 nodes. Under 20 is too shallow. Over 50 means you're documenting, not mapping.

### 3.2 Write Nodes

For each node:

1. Clear noun-phrase title
2. Opening sentence defines the concept, links to parent nodes
3. Core content with specific details (file paths, function names, numbers)
4. Embed `[N]` links naturally throughout — don't list them at the end
5. Explain WHY, not just WHAT

### 3.3 Weave Links

After drafting all nodes:

1. Check every node has 2+ outgoing links
2. Add backward links — if [5] mentions [12], ensure [12] mentions [5]
3. Verify link clusters form around major concepts
4. Test navigation: can you follow links from [1] to any deep node?

### 3.4 Add Header

Prepend the standard AI agent header (see [mindmap.md](mindmap.md)).

### 3.5 Verify

- [ ] All major systems have nodes
- [ ] Every significant file/component mentioned somewhere
- [ ] Git history captured with commit hashes
- [ ] Every node has 2+ links
- [ ] Important nodes have 5+ links
- [ ] Links are bidirectional where appropriate
- [ ] 20-50 nodes total
