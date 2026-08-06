# Integrating omp into Playbook — reuse the pi runtime, not the pi provider

> **Goal:** after a one-time per-project Playbook installation, a user can type
> bare `omp` at the project root and get task-gate blocking, gate echo, prompt
> logging, and session lifecycle hooks. No Playbook wrapper, explicit `-e`,
> credential handling, model allow-list, or replacement OMP profile is required.
> Nested-directory launch is a verified OMP limitation discussed below, not part
> of the current guarantee.
>
> **Status:** Phase A implemented and live-verified on OMP v17.2.9. Verified
> runtime facts are marked ✅, implemented decisions are marked ◆, and remaining
> Phase B or upstream-compatibility questions are marked ◇.

## 1. What is actually verified

### Extension loading

✅ OMP v17.2.9 auto-discovers a project extension from
`<project>/.omp/extensions/` in a bare `omp` launch. A scratch-project canary,
with no wrapper and no `-e`, observed both the extension factory and
`session_start` callback:

```text
FACTORY_CALLED (auto-discovered)
session_start
```

The same probe works under `omp -p`, so extension loading and lifecycle callbacks
also work in headless print mode. This proves that a wrapper is not required
**solely to load the extension**. It does not prove installation, plugin-cache
resolution, provider attribution, Phase B argument composition, JSON streaming,
sandbox dispatch, judge integration, or session handling.

### Discovery scope

✅ OMP v17.2.9 discovery is cwd-scoped. With the extension installed at
`<project>/.omp/extensions/`, launching from `<project>/a/b/c` did not load the
factory or fire `session_start`; adding a Git repository marker at the project
root did not change the result.

This is a silent enforcement gap: when OMP starts below the root, the bridge is
never loaded, so its ancestor-aware `findProjectRoot()` and fail-closed behavior
cannot run. The supported bare-command guarantee is therefore project-root-only
until a separate loading strategy is chosen.

### Runtime compatibility

✅ OMP uses the `@earendil-works/pi-coding-agent` `ExtensionAPI` surface used by
the existing Pi bridge. Prior probes observed the same `input`, `tool_call`, and
`tool_result` lifecycle and confirmed that `{block: true, reason}` denies a tool
call.

✅ The shared Bash policy remains reusable without provider-specific forks:

- `task-gate-hook`
- `state-echo-hook`
- `chat-log-hook`
- `session-start-hook`
- `session-end-hook`
- `gate-echo-lib.sh`

✅ A live `write` probe observed `tool_call` input keys `path,content`; returning
`{block: true, reason}` prevented the file from being created. This establishes
the native `write` path, not universal mutation coverage.

◇ Do not claim that OMP's complete tool surface exactly matches the current
bridge map. OMP help advertises `glob`, while the bridge currently maps `find`
and `ls`; OMP also exposes additional tools such as `python` and `notebook` that
can mutate files. Capture actual `event.toolName` and input shapes for every
tool whose behavior Playbook intends to classify. Bash/Python mutation remains
a known policy limitation unless explicitly brought into scope.

◇ The behavior of `agent_end`/`session_shutdown` as a blocking Stop equivalent
has not been established. Until an explicit veto probe succeeds, OMP must report
`has_stop_hook=False`.

## 2. Correct reuse boundary

The right abstraction boundary is the **Pi-compatible extension runtime**, not
`PiAdapter` as a provider:

| Layer | OMP decision |
|---|---|
| Bash policy | Reuse verbatim. |
| Extension event mapping and payload normalization | Share with Pi. |
| Provider identity and installation | OMP-specific configuration. |
| Pi wrapper, oMLX allow-list, and Pi config layout | Do not inherit. |
| Python provider adapter | Implement directly from `ProviderAdapter`; do not subclass `PiAdapter`. |

The shared bridge is provider-neutral. Provider selection now:

1. honor `PLAYBOOK_PROVIDER` when a wrapper supplies it (Pi);
2. otherwise read a Playbook-managed OMP metadata file in the project; and
3. reject an absent/unknown provider in a Playbook project rather than silently
   attributing OMP traffic to Pi.

The bridge implementation remains single-source. A small provider metadata file
is configuration, not a fork of hook policy.

## 3. Installation and lifecycle contract

Auto-discovery solves loading only after an extension exists. It cannot bootstrap
itself: if `.omp/extensions/playbook.ts` is absent or broken, no OMP callback runs
and `session-start-hook` cannot repair it.

◆ Therefore the supported contract is:

```text
pb-tasks init --provider omp        # one-time per project
omp                                 # bare launches thereafter, from project root
```

Existing Playbook projects need the same explicit migration step. A session
started through another already-working provider may refresh OMP artifacts as a
convenience, but that is repair behavior, not the bootstrap guarantee.

The bare command must currently be launched at the project root. Starting below
the root silently skips project extension discovery in OMP v17.2.9. Documentation
and startup UX make successful Playbook loading explicit: a root launch prints
`[Playbook] active (omp; project-root discovery)` after its session hook succeeds.

### Managed project artifacts

Provision exact, narrowly owned files:

```text
.omp/extensions/playbook.ts    # generated copy of the shared bridge
.omp/playbook.json             # provider identity + schema/version metadata
```

Prefer a managed copy over a symlink into the plugin cache. A cache symlink can
dangle after upgrade or reinstall. The copy is overwritten idempotently by the
installer/refresh path, so the source remains the shipped bridge and does not
become a maintained fork.

Do not ignore all of `.omp/`; users may keep project rules, agents, settings, or
other extensions there. Ignore only the exact Playbook-managed artifacts if they
are intentionally untracked. `uninstall_hooks()` must remove only those files and
prune directories only when empty.

### Multi-user scope

The OMP loader artifacts and hook installation are project-global because
`.omp/extensions/` itself is project-global. Runtime task/session state remains
user-specific through the canonical `resolve_agent_dir()` logic and
`.agent/current_user`.

Do not describe a project-global `.agent/hook-dir` as user-specific via
`resolve_agent_dir`; those are different layouts. If a pointer/cache file is
retained, define it explicitly as project-global installation metadata, not task
state.

## 4. Durable hook-script resolution

◆ The standalone OMP copy does not persist a plugin/cache path. It embeds OMP
identity, validates schema-2 `.omp/playbook.json`, and invokes the stable
machine launcher as `pb-tasks hook <script>`. The launcher resolves the current
central checkout after every install/update, so a Git pull needs no per-project
absolute-path rewrite. Missing metadata, an incompatible runtime schema, a
missing dispatcher, or an abnormal mutation hook fails closed.

The unspecialized shared bridge still accepts `PLAYBOOK_HOOK_DIR` for `pb-pi`,
whose wrapper deliberately supplies its central scripts directory. A project
development `scripts/` directory and old Marketplace caches are not OMP runtime
sources in the standalone contract.

### Fail-closed behavior

Today, failure to spawn a hook can produce `status === null`; the bridge blocks
only on exit status `2`, which can silently disable enforcement.

◆ In a recognized Playbook project, an unresolved or unspawnable
`task-gate-hook` must fail closed for mutating tool calls with a clear repair
message. Logging and post-tool failures should surface a visible diagnostic.
Outside a Playbook project the extension remains a no-op.

This failure behavior must be tested against a stale cached path and a removed
plugin-cache directory.

## 5. Explicit tradeoffs of bare OMP

Bare auto-discovery is a deliberate trade:

- Any project extension in `.omp/extensions/` is automatically executed by OMP.
  Playbook is using OMP's existing project-extension trust model, not introducing
  a new loader mechanism.
- `omp --no-extensions` is the emergency escape hatch. It disables Playbook
  enforcement as well as other extensions and must be documented as such.
- The Pi/oMLX model allow-list is not inherited. OMP owns provider/model choice;
  cost controls or fuzzy-model-selection policy remain an OMP concern.
- Pi's `<agent-dir>/pi/` configuration and session isolation are not inherited.
  Bare OMP uses its normal profile and session store, commonly under
  `~/.omp/agent/`. Playbook task state remains isolated, but OMP history and
  credentials are not Playbook-scoped.
- No hard Stop guard is advertised unless a real end-veto probe demonstrates it.

These are scope decisions, not evidence that the omitted capabilities are
unnecessary.

### Nested-launch design options

The nested-directory limitation cannot be repaired by project extension code,
because that code is precisely what OMP fails to load. Keep the alternatives
explicit until one is proven and selected:

1. **Root-only bare OMP:** smallest scope, but a weak enforcement default because
   launching below the root fails silently.
2. **User-level OMP extension registration:** potentially covers nested launches
   because the shared bridge already no-ops outside Playbook projects. This is
   not a free fix: it mutates user-global OMP state, executes Playbook loader code
   in every OMP session, and needs live proof of nested loading, project/global
   deduplication, version upgrades, uninstall, and multiple Playbook installs.
3. **A root-normalizing `playbook-omp` wrapper:** explicit and controllable, but
   gives up the bare-`omp` goal.
4. **Upstream ancestor discovery:** preserves the desired model but depends on an
   OMP change.

Do not adopt global registration merely because related configuration fields are
present in the binary. Its loading and lifecycle behavior must be exercised end
to end first.

## 6. Two implementation phases

### Phase A — interactive enforcement in bare OMP

This is the minimum feature promised by the goal:

1. Refactor the shared extension bridge to accept/read correct provider identity.
2. Add durable, validated hook-directory discovery and fail-closed handling.
3. Provision the managed OMP extension copy and metadata through
   `pb-tasks init --provider omp`.
4. Add an `OmpAdapter(ProviderAdapter)` sufficient for bootstrap, hook install /
   uninstall, interactive launch, and truthful capabilities.
5. Add narrowly scoped ignore and release rules for the managed artifacts.
6. Verify bare interactive OMP end to end in both development and installed
   plugin layouts.
7. Make the root-only loading precondition visible, or choose and verify one of
   the nested-launch strategies before claiming nested enforcement.

Phase A must not claim panel, judge, sandbox, or subagent support merely because
the adapter class exists.

### Phase B — first-class headless provider support

Only add this phase if OMP should participate alongside Claude, Codex,
Antigravity, and Pi in headless workflows:

- implement OMP-specific `headless_argv()` semantics for prompt, optional model,
  appended context, `bare`, and JSON streaming;
- implement `run_headless_judge()` and `launch_headless()` using agent key `omp`;
- register OMP in sandbox binary/bypass/home-state maps;
- register it in panel review, single-judge resolution, intent inference, and
  the subagent runner;
- add model aliases/provider synonyms only after deciding how OMP's
  multi-provider model selection should interact with existing Pi aliases;
- investigate OMP's real session-log format rather than inheriting Pi's
  `session_log_format="none"` assumption.

Do not inherit Pi's `panel_variants`, `run_headless_judge`, `launch_headless`,
oMLX provider flags, 100K context assumption, or session-log behavior.

### OMP CLI facts to pin

OMP v17.2.9 advertises `--model`, `--append-system-prompt`, `--mode`,
`--no-rules`, `--no-skills`, `--no-extensions`, `--hook`, and `-e`.
It does **not** advertise Pi's `--no-context-files`; do not place that flag in
`headless_argv()` without a successful direct compatibility probe. Decide which
OMP flags, if any, provide the intended clean-context semantics.

## 7. Verification plan

### Hermetic tests

- provider attribution is `omp`, never `pi`;
- install is idempotent and updates stale managed copies;
- uninstall removes only Playbook-owned artifacts;
- `.omp/` user files survive install/uninstall;
- legacy and multi-user Playbook layouts resolve runtime state correctly;
- hook-directory precedence and validation cover env, development, valid cache,
  stale cache, installed-plugin discovery, and complete failure;
- missing `task-gate-hook` fails closed for `edit` and `write`;
- actual OMP `event.toolName` and input shapes are pinned for `edit`, `write`,
  `glob`, and any additional mutating tool brought into scope;
- the bridge preserves Pi behavior after provider parameterization;
- release completeness includes every shared bridge and Bash-hook dependency;
- OMP arguments cover no-model, model, context, bare, and stream cases if Phase B
  is implemented.

Do not make the normal unit suite require a locally installed OMP binary.

### Optional live compatibility tests

When OMP is installed, probe:

1. project-root auto-discovery with no wrapper and no `-e`;
2. preserve the verified v17.2.9 nested-directory non-discovery result as a
   compatibility test, and update it if upstream behavior changes;
3. prompt logging with provider tag `omp`;
4. code edit without an active task is blocked;
5. code edit with an active task proceeds and receives gate echo;
6. installed-plugin layout, followed by simulated plugin upgrade/cache removal;
7. `--no-extensions` disables the Playbook bridge as documented;
8. whether any `agent_end`/shutdown event can veto turn completion;
9. whether OMP-spawned child agents inherit the project extension and gate
   enforcement.

If user-level registration is evaluated, separately probe nested loading,
duplicate callbacks when project and global copies coexist, version replacement,
uninstall, and behavior with multiple Playbook installations.

Record the tested OMP version. An `omp --help` contract probe should be optional
or skipped when OMP is unavailable, not a mandatory environment-dependent unit
test.

## 8. Phase A verification

Phase A is complete. The implementation and OMP v17.2.9 smokes verified:

1. `pb-tasks init --provider omp` provisions narrowly owned project files;
2. a subsequent bare `omp` launch from the project root loads the bridge without
   `-e` or a wrapper;
3. prompts log as OMP;
4. mutating code tools are blocked without an active task;
5. post-tool gate context appears with an active task;
6. legacy and multi-user task state both work;
7. stale hook-cache metadata cannot silently disable enforcement;
8. plugin upgrade/reinstall behavior is verified;
9. uninstall preserves unrelated `.omp/` content; and
10. capabilities truthfully report pre-tool, post-tool, prompt, and no Stop; and
11. the root-only constraint is visible to users, or a separately verified
    nested-loading strategy has replaced it.

Phase B has separate acceptance criteria for sandbox, judge, intent, and
subagent participation. Phase A must not be held hostage to those integrations,
and completing Phase A must not imply they exist.

## 9. Bottom line

OMP's project extension auto-discovery provides wrapper-free interactive
Playbook enforcement from the project root. The Bash policy is reused verbatim,
and the Pi-compatible event bridge remains single-source with provider-aware
identity, hook resolution, diagnostics, and repair guidance.

The integration is not merely a symlink and a `PiAdapter` subclass. It requires
a one-time installation contract, correct attribution, durable plugin discovery,
fail-closed behavior, narrow ownership of `.omp/` artifacts, and an adapter that
does not inherit Pi/oMLX semantics. First-class headless-provider wiring remains
an explicit, unimplemented second phase.
