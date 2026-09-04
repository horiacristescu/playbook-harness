---
description: Upgrade the Playbook Harness machine runtime
allowed-tools: [Bash, Read]
---
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/upgrade.md","source_hash":"9a596bf805f17a3e0d6b69af192ee1d74b88b66191bc6e9b0892b7156eec6e24"} -->
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/upgrade.md","source_hash":"01061adc900caf7b4fad3861493897fa2c60238a2f73ac85153ffa065b13fb8a"} -->
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/upgrade.md","source_hash":"176e62d5db13200d3ba12df25388cb328d138e04b4e4967af0cc993e6abc85f8"} -->
<!-- playbook-managed: {"managed_by":"playbook-harness","schema":2,"source":"skills/upgrade.md","source_hash":"4f99937b51d35fe59723d89984158f739496665c652c1dfa46e15ecce4ef17c1"} -->

# Upgrade Playbook Harness

Upgrade the standalone machine runtime without changing project files.

## Instructions

Run this command and stop on failure:

```bash
bash "${PLAYBOOK_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/playbook-harness}/install.sh" --upgrade
```

Then verify the installed tree and refresh this project’s copied provider
artifacts:

```bash
pb-tasks runtime-audit
pb-tasks init
```

Report the installed Git commit and the provider reconciliation summary.

If this machine still uses the old Claude Marketplace plugin, do not treat its
checkout as a standalone runtime. Remove the old Marketplace installation, run
the documented Playbook Harness curl installer, and then run `pb-tasks init`.
