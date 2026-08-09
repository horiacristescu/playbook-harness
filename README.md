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
| Claude Code | `CLAUDE.md`, `.claude/settings.local.json`, `.claude/.gitignore`, managed skills and commands | Bare terminal/IDE launch; warns if old Marketplace hooks may duplicate standalone hooks. |
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
pb-tmux-agent start reviewer codex -- --help
```

Provider launchers `pb-codex`, `pb-agy`, and `pb-pi` are namespaced to avoid
colliding with system commands. Bare Claude, Codex, and root-launched OMP use
their project-local integrations; use a wrapper only where it adds documented
session, sandbox, model, or extension behavior.

`pb-tmux-agent` is an optional persistent execution transport. It requires
`tmux` on `PATH`, but project initialization creates no tmux state and does not
change tmux configuration. Runs, logs, and results live under the XDG state
directory rather than inside a project:

```bash
pb-tmux-agent start reviewer command -- python3 -u -c 'print(input())'
pb-tmux-agent send reviewer "review this change"
pb-tmux-agent tail reviewer 50
pb-tmux-agent wait reviewer --timeout 30
pb-tmux-agent stop reviewer
```

Use `--namespace` to isolate campaigns and `--json` for controllers. Run
`pb-tmux-agent --help` for provider, working-directory, environment, and model
options. Hostile descendant containment remains the job of `pb-sandbox`.

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

   Init preserves `.agent/` tasks, sessions, chat history, `MIND_MAP.md`, and
   user-authored guidance. It refreshes only recognized Playbook-managed local
   hooks/assets for every supported agent detected on the machine. If guidance
   still uses `.claude/bin/tasks` or `.claude/bin/sandbox`, init preserves the
   original and creates a current `pb-*` proposal under `.agent/templates/`.
3. Verify the standalone runtime with `pb-tasks runtime-audit` and launch Claude
   in the project to confirm the local settings are active.
4. Remove the old plugin and Marketplace declaration:

   ```bash
   claude plugin uninstall playbook@claude-playbook-marketplace
   claude plugin marketplace remove claude-playbook-marketplace
   ```

Project files and task history are retained. If init reports possible duplicate
legacy hooks, remove only the old Playbook hook entries after verifying the new
local integration; never delete unrelated Claude hooks. Obsolete project-local
`.claude/bin/tasks` and `.claude/bin/sandbox` copies may be removed after all
guidance uses `pb-*`. Re-running init later is safe and adds local integration
for newly installed supported agents without scanning other projects or changing
global provider settings.

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
