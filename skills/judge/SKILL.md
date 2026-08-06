---
name: judge
description: >
  Blind evaluation pattern. Agent spawns an independent judge to evaluate
  an idea, plan, strategy, statement, or implementation. The judge sees
  the repo but not the conversation. Verdict goes to a shared markdown file.
argument-hint: <what to evaluate>
---

# Judge

## What It Does

Spawns a blind evaluator to judge something. The judge is a separate agent
that can read the full repo but has no access to the conversation. It forms
its own opinion from the artifacts alone.

**Use it to evaluate:** ideas, plans, strategies, statements, philosophies,
implementations, architectures, test suites, decisions, tradeoffs — anything
worth a second opinion.

## Why Blind?

The judge has no access to the conversation context. This prevents:
- Anchoring on the agent's framing
- Social pressure to agree with what was discussed
- Confirmation bias from seeing the agent's reasoning

The judge works from the same artifacts (code, docs, task.md) but reaches
its own conclusions.

**Always include `MIND_MAP.md` as the first file in the prompt.** It's the
project's institutional memory — architecture, decisions, history, and reasoning.
The judge starts a blank session with no conversation context. Without the mind
map it would spend ages exploring the repo to orient. With it, the judge gets
instant project context and can focus on evaluating, not exploring.

## How to Use

```
/judge <what to evaluate>
```

### 1. State what's being judged

Write it out in the conversation for the user. The judge won't see this —
it's for shared understanding between agent and user about what's on trial.

### 2. Write the judge prompt

The prompt must be **self-contained** — the judge has no prior context:

- **Subject** — what to evaluate, described from scratch
- **Files to read** — specific paths the judge should examine for grounding
- **Evaluation questions** — concrete, not open-ended
- **Output path** — where to write the verdict

### 3. Spawn the judge

```python
Task(
    subagent_type="general-purpose",
    description="Judge: <topic>",
    prompt=<self-contained prompt>
)
```

The judge starts fresh. No conversation history. Only what you put in the prompt
and what it reads from the repo.

### 4. Verdict file location (priority order)

1. **Task folder**: `.agent/tasks/NNN-name/judge-<topic>.md` — preferred
2. **docs/**: `docs/judge-<topic>.md` — if no active task
3. **/tmp/**: `/tmp/judge-<topic>.md` — last resort

### 5. Review together

Agent and user both read the verdict. Discuss which findings to act on,
which to park, which to dismiss with reasons.

## Prompt Template

Adapt to what you're evaluating. The role and questions change; the structure stays.

```
You are evaluating a {subject_type}. Ground your evaluation in the actual
code and artifacts — cite file:line references, not hypotheticals.

**Subject:** {what you're evaluating, described independently}

**Read these files:**
- {path_1} — {why}
- {path_2} — {why}

**Evaluation questions:**
1. {specific question}
2. {specific question}
3. {specific question}
4. What assumptions does this plan/implementation build on that aren't stated or verified?

**Write your verdict to:** {output_path}

Format your verdict as:
1. Numbered findings with evidence (file:line references)
2. A summary: what's the single biggest issue or strength?
```

### Role Examples

The judge's role should match what you're evaluating:

| Evaluating | Judge role |
|---|---|
| Testing strategy | Senior QA engineer who's seen projects ship bugs |
| Architecture plan | Staff engineer reviewing a design doc |
| Security approach | Penetration tester looking for gaps |
| Code implementation | Code reviewer on a strict team |
| Research findings | Academic reviewer checking methodology |
| Product decision | PM who asks "but what does the user need?" |
| Performance plan | SRE who's been paged at 3am |

## When to Use

- Before committing to a plan with high cost-to-reverse
- After stating a philosophy or strategy (test your own assumptions)
- When reviewing your own implementation (self-review is weak; blind review is stronger)
- When two approaches seem equivalent (let the judge break the tie)
- At reflection boundaries in a task (Checkpoint/Critique gates)

## Anti-Patterns

- **Vague prompts**: "Review this" gets vague reviews. Ask specific questions.
- **No file references**: A judge without paths gives opinions, not findings.
- **Ignoring verdicts**: If you dismiss every finding, stop using judges.
- **Judging trivia**: Reserve for things that matter. Not formatting choices.
- **Leaking context**: Don't paste conversation into the prompt. Blindness is the feature.
- **Multiple judges for one thing**: One judge per evaluation. If you disagree, argue back yourself — don't spawn another judge hoping for a different answer.
- **Verbose findings**: Eval data shows 3.5KB judge sections help (score 17), 4.7KB hurts (score 11). Max 5 findings, Critical/Important only. Each: 1-2 sentence problem + 1 sentence fix. The agent's attention budget is finite — concise findings steer, verbose findings become noise.
