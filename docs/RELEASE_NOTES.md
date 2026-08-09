# Playbook Harness release notes

## Legacy evaluator retirement — 2026-08-09

The old provider-specific evaluator has been retired before work begins on its
replacement. Removed material includes its worker, queue, campaign and judge
scripts; executable cases; stored run trees; hidden `PLAYBOOK_EVAL_CONFIG`
branches in task templates and hooks; and two tests that only described that
architecture.

The removal does not discard what was learned. `docs/eval-history.md` preserves
the campaigns' bounded conclusions, reusable case/rubric practices, provenance,
confounds, and superseded assumptions. Raw artifacts remain recoverable from
the development repository's Git history. Historical task and chat records are
unchanged and are not presented as a current test lane.

The public artifact audit now rejects retired evaluator runtime tokens. The
replacement is planned separately in `docs/eval-system-plan.md`: first ship a
general tmux execution substrate, then build Git-pinned historical cases and an
interactive arena on top.

## Testing and culture skills — 2026-08-09

This release adds two related but deliberately separate skills:

- `/testing` is the focused confidence workflow. It derives test needs from
  project promises, direct human corrections, architecture risks, and the
  existing suite; consults a compact cross-project testing culture; then
  recommends the one to five evidence upgrades whose benefit justifies their
  cost.
- `/culture` is the open-ended retrospective. It studies a project's lived
  history, lets forms of inheritance emerge from the evidence, traces lineage
  and supersession, and selects what future participants should carry.

Neither skill invokes the other automatically. `/testing` owns the bounded
testing method and its domain-specific `culture.md`; `/culture` can recover any
kind of project culture and keeps its own retrospective template.

The former `/intent-induced-testing` skill has been hard-renamed to `/testing`.
There is no compatibility alias or retained old directory; use `/testing` in
new prompts. Both complete skill trees ship in the central runtime and are
reconciled into Claude projects by `pb-tasks init`.

## Retired partial write log — 2026-08-07

The provider-dependent Write/Edit backup has been removed. It captured only
Claude-shaped structured writes (including normalized Pi/OMP calls), missed
Codex and shell writes, and its renamed data directory could occupy the default
runtime checkout path. Hooks and Linux sandbox setup no longer create or bind
that directory.

On a plain fresh install, an empty obsolete default root is reclaimed. If that
root contains only a real `write-logs/` tree, the installer creates a uniquely
named `~/Documents/playbook-write-logs-<UTC>.tar.gz`, reopens it, verifies every
regular-file byte and mode, then empties the retired root and installs. Siblings,
links, explicit/custom destinations, and verification failures are refused
without deleting source data. The separate legacy `~/.local/share/playbook`
archive is not swept automatically.

Release evidence before the final documentation-only rebuild:

- development code commit: `1023a2d`;
- audited public code commit: `1be2b584a45628ef640bd45ba6cca35aed659d04`;
- public artifact manifest SHA-256:
  `748fc8b780a205a5b89c072e5a9727513855f9597a4afec008a01c4c38c70550`;
- full regression: 1017 passed, 2 skipped;
- raw curl over empty and write-log-only default roots, verified Documents
  archive, trusted-clone install, repeat, project init, and post-init Write-hook
  non-recreation all passed in isolated macOS state.

## Post-cutover migration and lifecycle hardening — 2026-08-06

Existing Marketplace-era projects now have an explicit low-change transition:
install the central Harness, run `pb-tasks init` from each selected project
root, review any generated guidance proposal, then
remove only obsolete Playbook launchers/hooks. Reconciliation preserves
`.agent/` history, mind maps, user-authored guidance, credentials, foreign
hooks, and every unselected project. Installing another supported agent later
only requires rerunning init in projects where its local integration is wanted.

The implementation review also hardened four release boundaries:

- the public installer no longer accepts arbitrary `--repo` or `--ref` sources;
- release audit recomputes complete manifest schema/source/mode/hash records;
- interrupted fresh installs heal the complete owned `pb-*` launcher set on a
  normal repeat;
- public-checkout replacement pins the validated directory identity and uses
  no-follow descriptor-relative mutation, so a raced symlink cannot redirect
  deletion.

Hardening evidence:

- development code commit: `064cda2`;
- audited public code commit: `82b3774`;
- public code artifact manifest SHA-256:
  `1f27a942a9af8ffd13af1c42933c069f29432a4a94a1cfef756915b617ba5270`;
- focused migration/installer/artifact/publisher tests: 112 passed;
- full regression: 1008 passed, 3 skipped (three macOS nesting probes were
  rerun outside Codex's enclosing seatbelt and all 8 nesting tests passed).

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
