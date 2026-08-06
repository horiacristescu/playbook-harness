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

Migration:

- the former `claude-playbook-plugin` Marketplace repository is not a
  standalone predecessor and is never accepted or rewritten as an install
  origin;
- install Playbook Harness fresh, run `pb-tasks init` in each project, verify
  local hooks, then uninstall the old Claude plugin/Marketplace declaration;
- project `.agent/` history and provider-local files are preserved.

Known capability limits:

- OMP extension discovery is project-root/cwd scoped;
- Codex project hooks require the user-controlled global `codex_hooks` feature;
- Pi enforcement uses `pb-pi`;
- Antigravity 1.1.10 has no verified project-local hook loader and receives
  guidance only;
- provider version detection does not validate login state;
- successful reinstall retains no previous runtime checkout.
