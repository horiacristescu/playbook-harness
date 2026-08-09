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
