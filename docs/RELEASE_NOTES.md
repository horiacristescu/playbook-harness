# Playbook Harness release notes

## Narrative, natural gate editing, and migration repair — 2026-08-13

This release adds `pb-tasks narrative` and the `/narrative` skill. Agents author
incremental arcs and spans over project chat history; the CLI validates exact
`(timestamp, id)` boundaries and renders a local, self-contained page for the
user. Existing annotations remain hand-editable project meaning, while HTML and
parsed entries are derived and ignored. Narrative is deliberately one
observability lens—not a substitute for live session state, executable
provenance, or task authority.

Gate completion again uses ordinary provider file edits. Hooks validate that
only the authoritative first open gate advances, publish the transition, and
inject the next exact `task.md:line`; the short-lived agent-facing `task-edit`
command has been removed. Codex task closure now checkpoints authorized edits
before removing active ownership, preventing same-turn Stop hooks from
retroactively calling valid work unowned.

Standalone migration now distinguishes executable Marketplace consumers from
permission history, archived logs, and independent Claude worktrees. It still
refuses enabled legacy plugins and ambiguous live wiring before writes. Claude
startup and task hooks share provider-native UUID identity, and fresh managed
sessions surface interactive trust or hook-review prompts instead of silently
waiting. `pb-tasks doctor`, runtime generation reporting, named `pb-session`
status, and public-artifact audits expose the serving runtime and require the
Narrative instruction, implementation, and help surface to ship together.

The ordinary curl installer is now convergent. When it finds an authenticated
existing runtime, it stages and audits a fresh clone of current public `main`
and recoverably replaces the prior checkout. A successful repeat installation
therefore cannot leave an older commit presented as the current release;
candidate clone or audit failures preserve the prior audited runtime.

The installed provider launcher set now includes `pb-claude` alongside
`pb-codex`, `pb-agy`, and `pb-pi`, giving direct launches one predictable
`pb-<provider>` naming rule. The wrapper delegates Claude arguments unchanged
while scrubbing inherited agent identities and establishing project context.

Tmux remains an optional point-of-use dependency. `pb-session` and
`pb-tmux-agent` now diagnose its absence before reserving durable state and give
the agent exact permission/install/retry guidance; `pb-tasks init` remains
project-only and never installs machine packages.

## Tmux fleet-monitor skill and legacy retirement — 2026-08-12

The public artifact now ships one canonical `monitor` skill to every supported
provider integration's configured project skill path. One ordinary managed session can use
public `pb-session` and `pb-tasks` facts to observe and steer a fleet without a
special identity or control plane. The skill separates recorded lifecycle,
observed pane evidence, sent text, recipient acknowledgment, and demonstrated
action; silence is neither idleness nor completion, and task authority outranks
informal pane or plan claims.

The unused conversation-watcher implementation, copied project runtime, hook,
launcher, private state tree, and documentation corpus are no longer current or
public surfaces. Project initialization transactionally retires only exact
authenticated legacy launcher/hook/settings wiring. Modified files, foreign
hooks, task history, and recipient-authored data are preserved; ambiguous
active state refuses before writes. Clean and T120/T121 recipient fixtures reach
a byte-stable second init, and fresh-agent plus live three-worker tmux probes
exercise restraint, acknowledgment, retained exit evidence, and manual resume
orientation.

## Provider-native Playbook sessions — 2026-08-12

The standalone artifact now ships `pb-session`. It starts a supported provider
inside the existing tmux transport, binds the resulting provider-native
conversation ID to a durable Playbook record, and supports list, status, attach,
peek, send, exact resume, stop, rename, and destroy. Bare provider launches stay
valid: bootstrap records the same native identity as an ad-hoc session.

Session status distinguishes recorded lifecycle, observed tmux-body truth, task
authority, and sparse chat chronology. It retains the exact manual resume route
without storing another transcript or automatically resurrecting processes.

The public runtime now includes the complete session command and hook closure.
Project init recognizes exact Marketplace-era launchers and Playbook hook
entries, migrates them in one transaction, preserves foreign hooks, semantic
guidance, custom playbooks, and history, and refuses ambiguous active legacy
state before writing. Doctor inspects project migration state and the serving
runtime; runtime-audit remains project-independent. Immutable clean and
plugin-era recipient fixtures pass two init runs, doctor, runtime-audit, and
installed native-identity canaries for Claude, Codex, Agy, Pi, and OMP.
Legacy hook migration authenticates the complete frozen entry shape, and direct
consumer detection is limited to concrete runtime commands on operational
surfaces. Explicit Claude `enabledPlugins` activation blocks migration;
installation records and historical prose do not. Reconciliation revalidates
target bytes immediately before staging and replacement, and rollback never
overwrites a concurrently changed managed postimage.

## Interactive tmux substrate hardening — 2026-08-12

`pb-tmux-agent` now preserves the pane controlling terminal while giving the
interactive child its own foreground process group, so live resize and bounded
runner-authoritative cleanup coexist. Managed bodies use an isolated,
authenticated Playbook tmux server with one session/window/pane and wheel
bindings that operate tmux copy mode without forwarding to provider history.

Start waits for successful foreground exec. New `attach`, `detach`, and `peek`
commands complement serialized literal send plus separate Enter. Dead panes and
unexpected runner exits publish immutable status for later inspection;
ownership, topology, socket, and name mismatches refuse without collateral
cleanup. Tmux owns the foreground process group, not hostile `setsid()` escapes;
`pb-sandbox` remains the containment boundary.

## Interactive historical arena — 2026-08-09

Added frozen script/rubric/variant/campaign schemas, balanced opaque assignment,
hash-chained append-only evidence, literal controller barriers, exact tmux-owned cleanup,
post-run deterministic checks, blind cited command judges, multidimensional analysis, and
compact immutable `ADOPT`/`REJECT`/`RETEST` reports.

The exact-Nub `nub-mechanics` canary uses an explicitly authored script and two identical
local fixture arms, so its expected result is `RETEST`. It performs no model or network
call. Arena role packets reduce accidental leakage but are not an OS security sandbox;
real provider commands remain explicit and may carry privacy/cost implications.

## Portable historical cases — 2026-08-09

This release adds `pb-arena case list|prepare|doctor` and two real historical recipes.
Nub Markdown format is pinned to an exact project commit and task seed. Semlabel
dedup-hard is byte-exact to the former evaluator fixture while explicitly caveated
because its original-project commit was never recorded.

Reconstruction reads exact Git blobs from caller-bound local repositories, disables
replacement objects, ignores dirty worktrees, verifies patches/optional corpus
objects, rejects links, traversal, credential shapes and named future leakage, and
publishes only the expected normalized tree hash. Doctor double-builds and self-cleans.
The runtime owns code/manifests/patches only—not source repositories, prepared
workspaces, dependencies, tmux sessions, results, or large/private corpus objects.

## Persistent tmux agent transport — 2026-08-09

This release adds optional `pb-tmux-agent` lifecycle commands for durable,
provider-neutral agent execution. It supports owned namespaced starts, literal
multiline sends, append-only logs, bounded tails, status/result/wait, and exact
TERM-to-KILL cleanup. The runtime records atomic metadata and results outside
projects under the XDG state directory. Project init remains file-only and
does not install or configure tmux.

`tmux` is an optional external prerequisite. Claude, Codex, Pi, OMP, and Agy
resolution follows their existing launcher contracts; `command` accepts exact
argv for generic controllers and tests. Fresh installs, upgrades, launcher
repair, runtime audit, and uninstall now own `pb-tmux-agent` alongside the
other Harness commands.

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
