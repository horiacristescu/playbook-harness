# Playbook historical arena cases

This directory owns declarative historical-case recipes and the stdlib reconstruction
runtime. It does not own prepared workspaces, Git source repositories, credentials,
provider sessions, campaign results, or large/private corpus objects.

List the shipped cases:

```bash
pb-arena case list
```

Source identities are explicit and machine-local. For this development checkout:

```bash
pb-arena case doctor nub-markdown-format \
  --source code-monorepo=/path/to/Code

pb-arena case doctor semlabel-dedup-hard \
  --source playbook-development=/path/to/playbook-development
```

`case prepare CASE DEST` requires a destination that does not exist. It reads only the
pinned Git commit/subdirectory, applies verified case inputs, rejects known leakage,
and publishes a `.playbook-arena.json` provenance record. `case doctor` builds twice in
temporary directories and deletes them. Neither command runs historical tests,
installs dependencies, accesses the network, starts tmux, or mutates source projects.

Schema 1 uses strict JSON (`case.json`, `checksums.json`) to preserve the Harness's
Python-stdlib-only runtime. A caveated case can be byte-reproducible while explicitly
declining to claim exact original-project provenance.

## Interactive campaigns

Freeze before execution:

```bash
pb-arena campaign freeze ./campaign.json --results /absolute/private/results
```

Run with explicit local Git sources and the Playbook runtime repository:

```bash
pb-arena campaign run ./campaign.json \
  --results /absolute/private/results \
  --source source-id=/absolute/source/repo \
  --runtime-repo "$HOME/.local/share/playbook-harness"
```

The runner reconstructs a fresh workspace per frozen assignment, materializes one pinned
runtime variant, launches only through `pb-tmux-agent`, follows literal script triggers,
runs frozen checks after worker termination, gives independent command judges copied blind
packets, and writes an immutable `ADOPT`, `REJECT`, or `RETEST` report. Results are
append-only and caller-retained; an identity is never cleared for a rerun.

If the coordinator is interrupted during the tmux control phase, rerunning the frozen
campaign verifies its durable identity and reconnects without repeating a recorded send.
An ambiguous send or an interruption after evidence collection begins fails visibly;
partial campaigns remain resumable evidence and do not receive a final report.

The shipped network-free mechanics canary is:

```bash
pb-arena canary run nub-mechanics \
  --results /absolute/private/results \
  --source code-monorepo=/path/to/Code \
  --runtime-repo "$HOME/.local/share/playbook-harness"
```

It uses the exact Nub start but an explicitly authored one-message script and two
identical arms. Expected result: `RETEST`. It proves orchestration, not a Playbook effect
or historical conversation replay.

## Authority, privacy, and cost

- Role packets prevent accidental/cooperative leakage; they are not an OS security
  boundary against malicious commands running as the same account.
- Tmux exposes transport state only. The controller never infers questions, quiescence,
  correctness, or semantic completion from pane prose.
- Campaign argv is executable authority supplied by the author. Inspect it before run.
  Private temp directories, minimal environments, timeouts, pre-read workspace limits,
  and file-backed subprocess output bounds are
  containment hygiene, not a command sandbox.
- Real provider workers or judges may transmit project evidence and cost money. Nothing
  discovers credentials or launches them implicitly; the shipped canary is local Python.
- Token/currency cost remains explicit missingness until a trusted adapter supplies it.
  Cited judge output remains evidence, not hidden truth.
