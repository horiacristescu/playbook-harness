# Installation and upgrades

The Playbook runtime is installed once per machine. Projects are initialized
one at a time.

This separation keeps machine lifecycle work out of project directories and
keeps project changes local and reviewable.

## Requirements

- macOS or Linux
- Bash and Git
- Python 3.10 or newer
- `${XDG_BIN_HOME:-$HOME/.local/bin}` on `PATH`
- one or more supported agent CLIs, installed separately

Playbook does not install agents, log them in, or change provider-global
configuration.

## Install from the public release

```bash
curl -fsSL https://raw.githubusercontent.com/horiacristescu/playbook-harness/main/install.sh | bash
```

The default installation owns:

```text
${XDG_DATA_HOME:-~/.local/share}/playbook-harness/  central Git checkout
${XDG_BIN_HOME:-~/.local/bin}/pb-*                  managed launchers
${XDG_CACHE_HOME:-~/.cache}/playbook-harness/      disposable runtime cache
```

## Install from a clone

```bash
git clone https://github.com/horiacristescu/playbook-harness.git
cd playbook-harness
bash install.sh
```

This installs the exact checked-out public commit.

The source clone must be clean, on `main`, contain the audited artifact
manifest, and use a recognized public remote. A directory that merely resembles
the Harness is refused.

## Initialize a project

```bash
cd /path/to/project
pb-tasks init
```

`pb-tasks init [PROJECT]` reconciles files only beneath the explicit project,
or beneath the current directory when no path is given. It does not walk up and
initialize a parent directory.

Useful forms are:

```bash
pb-tasks init                         # all detected supported providers
pb-tasks init /path/to/project        # one exact project
pb-tasks init --provider codex        # one provider
pb-tasks init --no-hooks              # preserve hooks; refresh other assets
```

Detection is read-only and version-based. It does not make model requests or
prove that provider credentials are valid.

Rerun init after installing a new supported agent. Also rerun it after a
Playbook upgrade when copied project artifacts have changed.

Existing user-authored guidance is preserved. When an automatic merge is not
safe, init writes a marked proposal under `.agent/templates/` and returns a
clear conflict.

## Provider boundaries

| Provider | Project-local result | Current limit |
| --- | --- | --- |
| Claude Code | `CLAUDE.md`, local settings, managed skills and commands | Recognized Marketplace hooks migrate. Ambiguous legacy state must be resolved manually. |
| Codex | `AGENTS.md`, `.codex/hooks.json` | Codex requires `[features] hooks = true`. Init reads but never edits `~/.codex/config.toml`. |
| OMP | `AGENTS.md`, managed project extension and config | Bare OMP enforcement requires launch from the project root. |
| Pi | `AGENTS.md`, isolated project model and session state | Launch through `pb-pi` so the project extension and state are loaded. |
| Antigravity | `GEMINI.md` guidance | Agy 1.1.10 has no verified project-local hook loader, so support is guidance-only. |

Provider files coexist. Each agent reads its own project-local mechanism.
Playbook does not install a provider multiplexer.

## Upgrade and repair

From the default installation:

```bash
bash ~/.local/share/playbook-harness/install.sh --upgrade
bash ~/.local/share/playbook-harness/install.sh --repair-launchers
bash ~/.local/share/playbook-harness/install.sh --reinstall
bash ~/.local/share/playbook-harness/install.sh --uninstall
```

If you selected another XDG or explicit install directory, run that checkout's
`install.sh`.

A normal repeat install obtains and audits current public `main`, then replaces
the runtime through a recoverable staging path.

`--upgrade` works only from an authenticated, clean `main` checkout with an
upstream `origin/main`. It performs a fast-forward-only pull and audits before
and after. Live agents may observe the short in-place update window.

`--reinstall` performs the same fresh-clone replacement used by a normal
repeat install. It remains available for recovery and compatibility.

`--repair-launchers` recreates only marker-owned `pb-*` launchers.

`--uninstall` removes the machine runtime and managed launchers. It preserves
project-local tasks, memory, guidance, and hooks. There is no broad
project-uninstall command.

## Multiple projects and users

One machine runtime serves any number of projects.

Each project owns its own `.agent/` task and session state. Initialization in
one project does not scan or rewrite another.

Existing multi-user projects keep user task state under `.agent/<user>/`,
selected by `.agent/current_user`. Provider integration files remain
project-wide because provider processes load them from the project root.

## Move from the former Claude Marketplace release

The old `claude-playbook-plugin` repository is a retired Marketplace bundle.
It is not an upgrade source for the standalone Harness.

First install the current Playbook runtime. Then reconcile each existing
project:

```bash
cd /path/to/project
pb-tasks init
pb-tasks doctor
pb-tasks runtime-audit
```

Init preserves tasks, sessions, chat history, `MIND_MAP.md`, custom
playbooks, user guidance, and foreign hooks. It replaces exact generated
Marketplace launchers and owned Playbook hook entries. Ambiguous active legacy
dependencies are reported before project writes begin.

After the project is healthy, remove the retired plugin:

```bash
claude plugin uninstall playbook@claude-playbook-marketplace
claude plugin marketplace remove claude-playbook-marketplace
```

Exact generated `.claude/bin/tasks` and `.claude/bin/sandbox` scripts remain
as compatibility shims to the standalone commands. Modified scripts remain
user-owned and require manual resolution.

## Troubleshooting

If `pb-tasks` is not found, put
`${XDG_BIN_HOME:-$HOME/.local/bin}` earlier on `PATH`.

If another command shadows a `pb-*` launcher, the installer reports the
collision. Adjust `PATH` and run the launcher repair.

If the default install path contains only the retired `write-logs/` tree, a
fresh install archives it to a verified
`~/Documents/playbook-write-logs-<UTC>.tar.gz` file before replacing the old
directory. Any sibling entry, symlink, or unsupported file type is refused
without deleting the old data.

If upgrade reports a dirty checkout, inspect its `git status`. Do not reset it
blindly. Use reinstall only after understanding the local changes.

If project init reports a conflict, preserve the existing file, review the
proposal under `.agent/templates/`, resolve the overlap, and rerun init.

If Codex reports a prerequisite, enable its `hooks` feature yourself and
rerun init.

If OMP works at the project root but not below it, launch from the root or load
the extension explicitly. OMP 17.2.9 extension discovery is current-directory
scoped.

If a copied integration is stale, upgrade the machine runtime and rerun
`pb-tasks init` in that project.

For the authoritative installed version, run:

```bash
pb-tasks runtime-info
```

The reported Git commit is the runtime version.
