---
description: Upgrade the Playbook Harness machine runtime
allowed-tools: [Bash, Read]
---
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
