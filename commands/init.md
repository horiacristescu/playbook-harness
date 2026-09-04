---
description: Initialize or refresh this project for Playbook Harness
argument-hint: "[project-path]"
allowed-tools: [Read, Bash]
---
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/init.md","source_hash":"8bfc74af88b23b58c9977fd69cbcdf5e4350bf55330735d90eedbfce5a217cc6"} -->
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/init.md","source_hash":"90270fdcc36199a081ed1bd8a192ddf8b59ef06e5f0ad94d3365d830b1622499"} -->
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/init.md","source_hash":"8b06b0ee3318dc5b68f774ac1b309f29b674d8e920380da90d7e9c52faa6ed84"} -->
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/init.md","source_hash":"5ffa10f6b1cba071c2dd38d7efcef1ba427271bd2e567ba74b96316f0536df57"} -->

# Playbook Init

Reconcile project-local workflow files for every supported agent CLI installed
on this machine. Machine installation and provider login are separate concerns;
this command does not modify global provider settings or credentials.

## Instructions

From the intended project root, run one of:

```bash
pb-tasks init
pb-tasks init "/path supplied in $ARGUMENTS"
```

Use the second form only when an argument was supplied, preserving it as one
quoted path.

Stop on conflicts. Report every detected provider, whether its integration was
installed, updated, unchanged, prerequisite-only, wrapper-required, or
guidance-only, and surface all warnings. Do not overwrite an existing guidance
file manually; Playbook writes a proposal under `.agent/templates/` when human
merging is required.

Then run:

```bash
pb-tasks bootstrap
```

Rerun `pb-tasks init` after installing another supported agent or upgrading the
Harness when copied provider artifacts changed.
