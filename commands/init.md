---
description: Initialize or refresh this project for Playbook Harness
argument-hint: "[project-path]"
allowed-tools: [Read, Bash]
---
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
