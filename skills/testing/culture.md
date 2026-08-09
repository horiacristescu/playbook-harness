# Testing Culture Worth Transmitting

Read this after freezing the project's specification, human-signal, and architecture-risk ledgers. Use it as accumulated cross-project experience: challenge omissions, recognize recurring failure shapes, and reuse proven habits. Do not treat it as a source of project requirements. Pair every borrowed lesson with local evidence and an applicability check.

## What tests are for

Tests mediate among human intent, coding agents, code, and the external world. At their best they provide:

- **executable memory:** preserve why a failure mattered after the original conversation is gone;
- **delegated perception:** let an agent observe consequences it would otherwise ask a human to inspect;
- **attention compression:** pay the diagnosis cost once instead of making the human relive it;
- **bounded autonomy:** let agents move independently only as far as the evidence honestly supports.

A green suite is not the culture. The culture is the habit of turning promises, lived corrections, and foreseeable risks into trustworthy feedback.

## Practices worth transmitting

### Preserve the betrayal, not only the fix

Keep the smallest realistic input, topology, trace, timing condition, or interaction that produced lost trust. Name the user-visible failure. An abstract test that erases the incident may preserve implementation behavior while losing the reason for the test.

### Assert forbidden effects

Do not stop at the returned error or warning. Assert that the dangerous call, write, retry, disclosure, duplicate message, or state transition did not occur. For safety and preservation, test the world after rejection.

### Prove claims at the owning production boundary

Ask which layer owns the risky assumption and use the lowest boundary capable of disproving it. A mock cannot establish browser, provider, OS, Git, or database truth. Conversely, do not pay for end-to-end tests when a pure policy owns the rule.

Check for shadow paths: test-only adapters, reimplemented logic, code-derived fixtures, different thresholds, or entry points production never uses can create realistic-looking false confidence.

### Separate observation from authority

Telemetry should often survive observer failure. Enforcement must fail closed before the risky action. Do not give loggers, advisors, vetoes, and executors one undifferentiated failure contract. Test the consequence appropriate to the component's authority.

### Model lifecycle, identity, and interruption

Steady-state examples miss many expensive failures. Exercise start, retry, cancellation, restart, handoff, expiry, and concurrent transitions. Carry stable identities and provenance through aggregation; equal values or plausible counts do not prove that the correct entities were used.

For persistence, migration, release, and archive work, inject failure between steps. Require recovery to preserve the prior valid state. When nonempty durable state is unreadable, refusing “helpful recovery” may be safer than overwriting the only evidence.

### Keep confidence states honest and current

Distinguish:

- narrative memory;
- executable-open fixtures, skips, or xfails;
- controlled regressions;
- owner-boundary proof;
- historical campaigns;
- current live canaries;
- manual or human-owned judgment.

Evidence decays. A stale test map, untracked regression, excluded test-shaped file, silent optional probe, or vestigial test must not inherit current confidence. Retire obsolete tests deliberately when the protected behavior is deliberately retired.

### Calibrate consequential tests

For an important regression, temporarily introduce the named defect, require the test to fail for the intended reason, and revert the mutation. This checks whether the executable observer can see its claimed betrayal. Use targeted calibration, not mutation-score accumulation.

### Keep semantic claims semantic

Schema validity, enum membership, vector shape, and numeric bounds do not prove usefulness, coherence, or meaning. Layer mechanical checks with named positives and hard negatives, metamorphic cases, frozen provenance, and small blinded or human evaluation when judgment owns the final claim.

### Make failure useful to the next agent

A test should identify the violated invariant and likely owning layer without exposing private details or requiring the human to reconstruct the story. Separate product, environment, protocol, and test-harness failure before weakening a contract.

## Reuse protocol

For each cultural lesson that appears relevant:

1. Cite the local specification, human signal, or architecture evidence that makes it applicable.
2. State the failure it would prevent and the boundary that owns it.
3. Choose the cheapest proof that would materially improve confidence.
4. Record limits, flakiness, credentials, semantic judgment, and maintenance cost.
5. Mark the lesson `ADOPT`, `PARK`, or `REJECT` for this project with a reason.

Independent local discovery comes first. Shared culture is a prior and a challenge set, not a substitute for listening to the current project or human.

## Habits worth retiring

- Equating test count, green status, or coverage percentage with user confidence.
- Asserting a warning without checking preservation or forbidden side effects.
- Presenting mock-owned behavior as external-platform truth.
- Treating historical/manual evidence as an active regression lane.
- Letting xfails preserve an indefinitely worsening failure envelope.
- Mistaking generated summaries or prompts for direct human intent.
- Assuming a file runs because its name looks like a test.
- Deriving tests only from module layout, only from specifications, or only from expressed pain.
- Automating irreducibly human judgment with a convenient false oracle.
- Importing a cross-project lesson without demonstrating local applicability.

The cultural objective is not more testing. It is enough durable, well-placed evidence that future agents can work with greater independence without disconnecting from human experience or making the human pay for the same lesson again.
