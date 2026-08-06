# Playbook Harness release notes

## Standalone public cutover — 2026-08-06

This is the first standalone Playbook Harness release on rolling public
`main`. The authoritative release identity is the Git commit reported by
`pb-tasks runtime-info`.

Highlights:

- one central Git runtime installed by curl or trusted clone;
- namespaced `pb-*` commands with no generic system-command collisions;
- separate project-local `pb-tasks init` for all detected supported agents;
- Claude and Codex project hooks, root-scoped bare OMP enforcement, Pi wrapper
  integration, and honest Antigravity guidance-only status;
- audited install, repeat, fast-forward upgrade, staged reinstall, repair, and
  uninstall lifecycles;
- deterministic public artifact membership and installed-tree self-audit;
- multi-project and existing multi-user task-state support.

Release evidence:

- public repository: <https://github.com/horiacristescu/playbook-harness>;
- authoritative development code commit: `8ebb756`;
- audited public code commit: `a887c9c`;
- public artifact manifest SHA-256:
  `50e993ed55fbe6fdbd538ce7b26265cf80403ceeab5e6fb12f824bab4d5a7943`;
- legacy retirement commit: `62262a5` in the archived former Marketplace
  repository;
- final pre-documentation regression: 1000 passed, 2 skipped;
- real release smokes: macOS Bash 3.2 and Ubuntu 26.04/aarch64, including raw
  curl, trusted clone, repeat, fast-forward upgrade, staged reinstall,
  uninstall, and multi-project init.

The Linux smokes found and fixed both permissive-umask boundaries: installer
clones now create exact `0644/0755` runtime modes, and a clean user clone with
`0664/0775` source modes is accepted without mutation and normalized in the
audited staging clone.

Migration:

- the former `claude-playbook-plugin` Marketplace repository is not a
  standalone predecessor and is never accepted or rewritten as an install
  origin;
- install Playbook Harness fresh, run `pb-tasks init` in each project, verify
  local hooks, then uninstall the old Claude plugin/Marketplace declaration;
- project `.agent/` history and provider-local files are preserved.

Known capability limits:

- OMP extension discovery is project-root/cwd scoped;
- Codex project hooks require user-controlled `[features] hooks = true`;
- Pi enforcement uses `pb-pi`;
- Antigravity 1.1.10 has no verified project-local hook loader and receives
  guidance only;
- provider version detection does not validate login state;
- successful reinstall retains no previous runtime checkout.
