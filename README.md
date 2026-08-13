# Playbook Harness

Install the machine runtime once, then initialize each project:

```bash
curl -fsSL https://raw.githubusercontent.com/horiacristescu/playbook-harness/main/install.sh | bash
cd /path/to/project
pb-tasks init
```

Playbook Harness gives coding agents a durable task plan, project memory, hook
enforcement, blind reviews, and contained subagents. It is provider-neutral and
does not require the Claude Marketplace.

## Requirements

- macOS or Linux
- Bash, Git, and Python 3.10 or newer
- `~/.local/bin` on `PATH` (or set `XDG_BIN_HOME`)
- one or more supported agent CLIs, installed separately

The installer does not install agents, log them in, initialize the current
directory, or change provider-global configuration.

## Install from a clone

The clone flow installs the exact checked-out public commit and is equivalent
to the curl flow:

```bash
git clone https://github.com/horiacristescu/playbook-harness.git
cd playbook-harness
bash install.sh
```

The source clone must be clean, on `main`, contain the audited artifact
manifest, and use a recognized public remote. A directory that merely resembles
the Harness is refused.

## Machine install versus project init

Machine install owns only:

```text
${XDG_DATA_HOME:-~/.local/share}/playbook-harness/  central Git checkout
${XDG_BIN_HOME:-~/.local/bin}/pb-*                  managed launchers
${XDG_CACHE_HOME:-~/.cache}/playbook-harness/      disposable runtime cache
```

`pb-tasks init [PROJECT]` performs only local file reconciliation beneath the
explicit project path, or beneath the current directory when no path is given.
It creates the shared `.agent/` task state, `MIND_MAP.md`, and provider-specific
guidance/hooks for every installed CLI whose version is supported. It does not
walk up and initialize a parent directory.

Detection is read-only and version-based. It does not perform login or model
requests, so “installed and supported” does not prove that provider credentials
are currently valid. Login remains the provider’s responsibility.

Useful init forms:

```bash
pb-tasks init                         # all detected supported providers
pb-tasks init /path/to/project        # one exact project
pb-tasks init --provider codex        # reconcile only one provider
pb-tasks init --no-hooks              # preserve hook files; refresh other assets
```

Rerun init after installing a new supported agent. It adds that provider
without removing existing integrations. Also rerun it after an upgrade when
copied artifacts—especially the OMP bridge—changed.

Existing user-authored guidance is preserved. When automatic incorporation is
unsafe, init writes a marked proposal under `.agent/templates/` and reports
that manual merging is required.

## Provider contracts

| Provider | Project-local result | Current limit |
| --- | --- | --- |
| Claude Code | `CLAUDE.md`, `.claude/settings.local.json`, `.claude/.gitignore`, managed skills and commands | Bare terminal/IDE launch; recognized old Marketplace hooks migrate, while ambiguous/global legacy state is reported as incomplete. |
| Codex | `AGENTS.md`, `.codex/hooks.json` | Codex must have `[features] hooks = true`; init reads but never edits `~/.codex/config.toml`. |
| OMP | `AGENTS.md`, `.omp/extensions/playbook.ts`, `.omp/playbook.json` | Bare `omp` is enforced only when launched at the project root. `omp --no-extensions` is the emergency bypass. |
| Pi | `AGENTS.md`, `.agent/pi/config/models.json`, `.agent/pi/sessions/` | Launch with `pb-pi`; the wrapper supplies extension loading and isolated project state. |
| Antigravity (`agy`) | `GEMINI.md` guidance | Agy 1.1.10 has no verified project-local hook loader, so init reports guidance-only and never installs a global plugin. |

Provider files coexist: each agent reads only its own project-local mechanism.
Project init does not install a cross-provider multiplexer.

## Everyday commands

```bash
pb-tasks bootstrap                    # mind map, pending tasks, CLI reference
pb-tasks new <type> <name> [intent]   # create a task
pb-tasks work <N>                     # activate one task
pb-tasks work done                    # finish it when all gates are checked
pb-tasks plan-review <N>              # blind plan review
pb-tasks impl-review <N>              # blind implementation review
pb-tasks doctor                       # project/harness diagnostics
pb-tasks runtime-audit                # verify installed artifact integrity
pb-tasks runtime-info                 # authoritative runtime schema + Git commit
pb-sandbox --prompt "..." --agent codex
pb-session                            # start the default provider in managed tmux
pb-session list                       # recorded lifecycle and observed body state
pb-session status <name-or-native-id> # exact manual resume route after interruption
pb-tmux-agent start reviewer codex -- --help
pb-arena case list                     # inspect portable historical cases
pb-arena canary list                   # inspect shipped network-free canaries
```

Provider launchers `pb-codex`, `pb-agy`, and `pb-pi` are namespaced to avoid
colliding with system commands. Bare Claude, Codex, and root-launched OMP use
their project-local integrations; use a wrapper only where it adds documented
session, sandbox, model, or extension behavior.

`pb-session` composes provider-native conversation identity with an optional
human name and one managed tmux body. A zero-argument launch uses the saved
project provider (Codex by default) and attaches; direct provider launches remain
supported and bootstrap records them as ad-hoc sessions. `list` and `status`
separate durable lifecycle from live-body observation and print an exact resume
route, so a reboot does not require remembering which conversations were open.
The session record is orientation state, not a transcript or automatic process
resurrection.

For a managed fleet, ask one ordinary Playbook session to monitor the others.
Project initialization places the `monitor` skill in each selected provider's
configured project skill path from canonical `skills/monitor/`. It teaches the agent to inventory, inspect,
and steer through `pb-session`; distinguish recorded, observed, sent,
acknowledged, and acted-on evidence; respect task authority; and leave healthy
work alone. Monitoring adds no daemon, private state tree, transcript scraper,
or special agent identity. Project-specific assignment and approval rules remain
ordinary operator-supplied Markdown.

`pb-tmux-agent` is an optional persistent execution transport. It requires
`tmux` on `PATH`, but project initialization creates no tmux state and does not
change the user's ordinary tmux server or configuration. Managed bodies use a
separate Playbook-owned tmux server; runs, logs, and results live under the XDG
state directory rather than inside a project:

```bash
pb-tmux-agent start reviewer command -- python3 -u -c 'print(input())'
pb-tmux-agent send reviewer "review this change"
pb-tmux-agent peek reviewer 50
pb-tmux-agent attach reviewer             # from an ordinary, non-tmux terminal
pb-tmux-agent detach reviewer
pb-tmux-agent tail reviewer 50
pb-tmux-agent wait reviewer --timeout 30
pb-tmux-agent stop reviewer
```

Use `--namespace` to isolate campaigns and `--json` for controllers. Run
`pb-tmux-agent --help` for provider, working-directory, environment, and model
options. Each body is exactly one session/window/pane; readiness follows
successful foreground exec, pane resize reaches the child, wheel events stay in
tmux scrollback, and dead panes retain exact exit evidence. Cleanup owns the
foreground process group only. A child that escapes with `setsid()` is outside
that group, so hostile descendant containment remains the job of `pb-sandbox`.

`pb-arena` reconstructs Git-pinned historical project moments for evaluation without
touching live project worktrees. Cases bind logical source IDs explicitly, verify
patches and prepared-tree hashes, reject named future leakage and credential shapes,
and distinguish exact from caveated provenance. `case doctor` reconstructs twice and
self-cleans; it does not run agents, tests, dependencies, tmux, or network operations.
Frozen `campaign run` is a separate explicit authority: it executes declared commands
through tmux, retains append-only evidence, and may carry provider privacy/cost when the
campaign declares a real agent. The shipped `nub-mechanics` canary uses local fixtures
only and is expected to return `RETEST`.
See `arena/README.md` in the runtime for examples and ownership boundaries.

## Upgrade, repair, reinstall, uninstall

```bash
bash ~/.local/share/playbook-harness/install.sh --upgrade
bash ~/.local/share/playbook-harness/install.sh --repair-launchers
bash ~/.local/share/playbook-harness/install.sh --reinstall
bash ~/.local/share/playbook-harness/install.sh --uninstall
```

Those commands show the default location. If you selected an XDG or explicit
install directory, run that checkout's `install.sh` instead.

- Normal reruns report the existing installation; they never upgrade silently.
- Upgrade requires an authenticated, clean `main` checkout with upstream
  `origin/main`, performs only `git pull --ff-only`, audits before and after,
  and warns that live agents may observe the short in-place update window.
- Reinstall clones and audits a sibling replacement before swapping it in. It
  does not retain an older runtime version after success.
- Repair recreates only marker-owned `pb-*` launchers.
- Machine uninstall removes the managed runtime and launchers, but deliberately
  preserves every project-local file and hook.

There is no broad project-uninstall command in this release. Back up the
project and inspect provider-local ownership markers before removing local
integrations manually.

## Multiple projects and users

One machine runtime serves any number of projects. Each project owns independent
`.agent/` task/session state and provider files; init in one project does not
scan or rewrite another. Existing multi-user projects keep user task state under
`.agent/<user>/`, selected by `.agent/current_user`, while provider integration
files remain project-wide because provider processes load them from the root.

## Migrating from the Claude Marketplace release

The old `claude-playbook-plugin` repository is a different Marketplace bundle,
not a trusted standalone runtime. Playbook Harness never rewrites that checkout
or accepts it as an upgrade origin.

1. Install Playbook Harness using curl or the trusted-clone flow above.
2. In each existing project, apply local reconciliation:

   ```bash
   cd /path/to/project
   pb-tasks init
   ```

   Init preserves `.agent/` tasks, sessions, chat history, `MIND_MAP.md`, custom
   playbooks, user-authored guidance, and foreign hooks. It atomically replaces
   exact generated Marketplace launchers and Playbook hook entries with the
   standalone forms. Semantic guidance is preserved and receives a current
   `pb-*` proposal under `.agent/templates/` when automatic adoption is unsafe.
   An ambiguous active legacy dependency makes init return nonzero before any
   project write; resolve the reported surface and rerun init.
3. Run `pb-tasks doctor` in the project and `pb-tasks runtime-audit` from any
   directory. Doctor classifies project migration state; runtime-audit checks
   only the immutable serving runtime.
4. Remove the old plugin and Marketplace declaration:

   ```bash
   claude plugin uninstall playbook@claude-playbook-marketplace
   claude plugin marketplace remove claude-playbook-marketplace
   ```

Project files and task history are retained. Exact generated project-local
`.claude/bin/tasks` and `.claude/bin/sandbox` launchers remain as compatibility
shims to `pb-tasks` and `pb-sandbox`; modified scripts remain foreign and require
manual resolution. Re-running init is a no-write fixed point after convergence
and can add integration for newly installed supported agents without scanning
other projects or changing global provider settings.

## Troubleshooting

- **`pb-tasks` not found:** add `${XDG_BIN_HOME:-$HOME/.local/bin}` to `PATH`.
- **Old write logs occupy the install path:** a plain fresh install recognizes
  only an otherwise-empty default root or a sole real `write-logs/` tree. It
  archives the latter to a verified, uniquely named
  `~/Documents/playbook-write-logs-<UTC>.tar.gz`, empties the retired directory,
  and then installs. Any sibling file or symlink is refused without deletion.
- **Another command shadows `pb-*`:** place the managed bin directory earlier
  on `PATH`; the installer reports the resolved collision.
- **Upgrade says dirty:** inspect `git status` in the installed checkout. Do not
  reset it blindly; use `--reinstall` only after understanding local changes.
- **Init reports a conflict:** the target is user-owned or locally modified.
  Preserve it, review the generated proposal, and rerun init after resolving it.
- **Codex says prerequisite:** enable Codex’s `hooks` feature yourself,
  then rerun init. Harness does not change the global setting.
- **OMP works at root but not below it:** OMP 17.2.9 extension discovery is
  cwd-scoped. Launch from the project root or explicitly load the extension.
- **Copied integration is stale:** upgrade the machine runtime, then rerun
  `pb-tasks init` in that project.
- **Need the exact installed version:** `pb-tasks runtime-info` reports the Git
  commit; that commit, not a semantic version string, is authoritative.

See [release notes](docs/RELEASE_NOTES.md) and the
[OMP integration details](docs/integrating-omp.md).
