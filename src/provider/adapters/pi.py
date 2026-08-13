"""
PiAdapter — provider adapter for the `pi` CLI (https://github.com/pi-os/pi).

In Playbook, pi is driven against a local LLM served by oMLX
(https://omlx.app/) — typically Qwen3.6-35B-A3B-4bit on Apple Silicon at
127.0.0.1:8000. The combination is useful for **volume work that is not time
sensitive** (PDF corpus labeling, batch evals) where hosted CLIs would be
slow or expensive. Useful up to ~100K context; beyond that, prefer claude
or codex.

Bootstrap file: AGENTS.md (pi reads it the same way codex does).

Hook surface: pi (0.73+, verified live on 0.80.2) HAS an extension system
(`pi -e <adapter.ts>`). The Playbook bridge `scripts/playbook-pi-hook-adapter.ts`
maps pi lifecycle events to the existing bash hooks, giving a pi session real
PreToolUse gate-blocking, post-tool gate echo, and prompt logging — the same
prevention loop claude/codex/agy have. It is loaded by the `bin/playbook-pi`
wrapper via `-e`, so hook install is wrapper-owned (no global config mutation);
`install_hooks` only provisions the per-project `<agent-dir>/pi/` config. No
blocking Stop equivalent yet (pi can't refuse to end a turn) — anti-walk-away
enforcement is deferred (Phase 2).

Headless invocation: `pi -p "<prompt>"` (or `--print`). Optional flags we
care about: `--provider <name>` (default `google`; we usually want `oss`
to hit the local oMLX endpoint), `--model <id>`, `--no-context-files`
(disable AGENTS.md/CLAUDE.md auto-load — useful for clean judge runs),
`--no-tools` (disable tools entirely), `--append-system-prompt`.

Pi reads its provider/model config from `~/.pi/agent/models.json` +
`settings.json` — oMLX writes these files before exec'ing pi. So a
production pi invocation in Playbook typically goes through `omlx launch
pi --` so omlx owns the config-file lifecycle.

Panel-review participation: single variant (None) — oMLX serves one model
at a time, so there's no point cycling models per panel slot.
"""

from __future__ import annotations
import os
import subprocess
from pathlib import Path
from typing import Optional

from ..adapter import ProviderAdapter, Invocation
from ..capabilities import ProviderCapabilities
from ..session_identity import SessionConformance, declared_session_conformance


class PiAdapter(ProviderAdapter):
    """Provider adapter for the `pi` CLI driving a local oMLX-hosted model
    OR a hosted provider (OpenRouter) via pi's `--provider` flag."""

    #: Practical context limit before the LOCAL pi+Qwen variant degrades
    #: noticeably (per user report). Hosted variants (e.g. or-pi → DeepSeek)
    #: have their own larger limits set by the upstream provider.
    CONTEXT_LIMIT_TOKENS: int = 100_000

    #: Panel-variant label → (pi `--provider` value, pi `--model` value or None).
    #: `None` is the default local variant (oMLX serving Qwen at 127.0.0.1:8000).
    #: Add new hosted variants here — `panel_variants()` decides availability.
    _VARIANT_MAP: dict[Optional[str], tuple[str, Optional[str]]] = {
        None: ("oss", None),
        "or-pi": ("openrouter", "deepseek/deepseek-v4-flash"),
    }

    def __init__(self, session_id: str, project_root: Path) -> None:
        self._session_id = session_id
        self._project_root = project_root

    # ── CLI identity ─────────────────────────────────────────────────────────

    @classmethod
    def binary_name(cls) -> str:
        return "pi"

    @classmethod
    def is_available(cls) -> bool:
        """pi can be driven directly OR via `omlx launch pi` — either suffices.
        Override default `which(binary_name())` so panel discovery doesn't
        silently drop pi when only omlx is installed.
        """
        import shutil
        return shutil.which("pi") is not None or shutil.which("omlx") is not None

    @classmethod
    def panel_variants(cls) -> list[Optional[str]]:
        # Local variant (None) always available if pi is installed.
        # Hosted variants gated on the relevant API key being in env —
        # the agent inherits env from the parent shell, so a key in `bash`
        # is visible to pi without any config file.
        variants: list[Optional[str]] = [None]
        if os.environ.get("OPENROUTER_API_KEY"):
            variants.append("or-pi")
        return variants

    def run_headless_judge(
        self,
        prompt: str,
        model: Optional[str],
        system_context: str,
        *,
        web_search: bool,
        timeout_secs: int,
    ) -> str:
        import shutil
        # Prefer omlx (handles config-file setup via os.execvpe), fall back to
        # direct pi if omlx is missing.
        if shutil.which("omlx") is None and shutil.which("pi") is None:
            return f"(error: neither omlx nor pi found on PATH)"
        inv = self.headless_argv(prompt, model, context=system_context)
        from provider.session_identity import scrub_inherited_session_identity
        env = scrub_inherited_session_identity(os.environ)
        env["PLAYBOOK_PROVIDER"] = "pi"
        env["PLAYBOOK_SESSION_ID"] = self._session_id or "judge"
        env["PLAYBOOK_ROLE"] = "noninteractive"
        from provider import sandbox as _sandbox
        result = _sandbox.run(
            "pi", inv.argv,
            project_root=self._project_root,
            env=env,
            capture_output=True, text=True, timeout=timeout_secs,
        )
        return _sandbox.format_judge_output(result)

    def headless_argv(
        self,
        prompt: str,
        model: Optional[str],
        *,
        context: str = "",
        bare: bool = False,
        stream: bool = False,
    ) -> Invocation:
        # Resolve variant label → (--provider, --model). Unknown labels are
        # treated as raw model identifiers routed via openrouter (so callers
        # can pass `provider/model` strings ad-hoc). pi is model-agnostic:
        # "which model" is just these flags, not a different binary.
        provider, model_id = self._VARIANT_MAP.get(model, ("openrouter", model))
        argv = ["-p", prompt, "--provider", provider, "--no-context-files"]
        if model_id:
            argv += ["--model", model_id]
        if context and not bare:
            argv += ["--append-system-prompt", context]
        if stream:
            argv += ["--mode", "json"]
        return Invocation(argv)

    # ── Identity ─────────────────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def project_root(self) -> Path:
        return self._project_root

    # ── Bootstrap ────────────────────────────────────────────────────────────

    def bootstrap_file_name(self) -> str:
        # pi auto-loads AGENTS.md from the project cwd (same convention codex
        # uses). Reuse the same content.
        return "AGENTS.md"

    def install_bootstrap(self, project_root: Path) -> None:
        """Write AGENTS.md (shared with codex) if not present."""
        from tasks.template import agents_md_template
        target = project_root / "AGENTS.md"
        if not target.exists():
            target.write_text(agents_md_template(), encoding="utf-8")

    # ── Hooks ─────────────────────────────────────────────────────────────────
    # pi (0.73+) HAS an extension system (`pi -e <adapter.ts>`). The Playbook
    # hook bridge (`scripts/playbook-pi-hook-adapter.ts`) maps pi lifecycle
    # events to the existing bash hooks: tool_call→task-gate-hook (a `{block}`
    # return = a real PreToolUse deny), tool_result→state-echo-hook (gate echo),
    # input→chat-log-hook, session_start/shutdown→session hooks. The adapter is
    # loaded by the `bin/playbook-pi` wrapper via `-e`, so hook installation is
    # *wrapper-owned* and needs no global config mutation. install_hooks only
    # provisions the per-project pi config dir + model allow-list file under the
    # resolved agent dir; uninstall removes it. (Verified live, pi 0.80.2, T145.)

    @staticmethod
    def _shipped_models_json() -> Optional[Path]:
        """Locate the shipped oMLX model allow-list (`playbook-pi-omlx-models.json`).
        Lives in `scripts/` next to the hook scripts in both dev and installed
        layouts — walk up from this module looking for it."""
        here = Path(__file__).resolve()
        for parent in here.parents:
            cand = parent / "scripts" / "playbook-pi-omlx-models.json"
            if cand.exists():
                return cand
        return None

    def _pi_config_dir(self, project_root: Path) -> Path:
        from tasks.core import resolve_agent_dir
        return resolve_agent_dir(project_root) / "pi"

    def install_hooks(self, project_root: Path) -> None:
        """Provision the per-project pi config under `<agent-dir>/pi/`.

        Idempotent. Writes nothing under $HOME — the extension is loaded via the
        wrapper's `-e` flag, so there is no global hook state to mutate. Honors
        `.agent/current_user` (multi-user) via `resolve_agent_dir`.
        """
        import shutil
        pi_dir = self._pi_config_dir(project_root)
        config_dir = pi_dir / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (pi_dir / "sessions").mkdir(parents=True, exist_ok=True)
        src = self._shipped_models_json()
        if src is not None:
            shutil.copyfile(src, config_dir / "models.json")

    def uninstall_hooks(self, project_root: Path) -> None:
        """Remove only the provisioned per-project pi config."""
        import shutil
        pi_dir = self._pi_config_dir(project_root)
        if pi_dir.exists():
            shutil.rmtree(pi_dir, ignore_errors=True)

    # ── Launch ───────────────────────────────────────────────────────────────

    @staticmethod
    def _wrapper_path() -> Optional[Path]:
        """Locate the public `pb-pi` or development `playbook-pi` launcher."""
        here = Path(__file__).resolve()
        for parent in here.parents:
            for sub in ("bin", "scripts"):
                for name in ("pb-pi", "playbook-pi"):
                    cand = parent / sub / name
                    if cand.exists():
                        return cand
        return None

    @classmethod
    def _interactive_support_error(cls) -> Optional[str]:
        """Return why the wrapper-backed interactive path is unavailable."""

        import shutil

        wrapper = cls._wrapper_path()
        if wrapper is None or not wrapper.is_file():
            return "Playbook pi wrapper/identity bridge is not installed"
        candidate_hook_dirs = (wrapper.parent, wrapper.parent.parent / "scripts")
        required = (
            "gate-echo-lib.sh",
            "task-gate-hook",
            "playbook-pi-hook-adapter.ts",
            "playbook-pi-omlx-models.json",
        )
        if not any(
            all((hook_dir / name).is_file() for name in required)
            for hook_dir in candidate_hook_dirs
        ):
            return "Playbook pi wrapper is missing required identity bridge assets"
        if shutil.which("pi") is None:
            return "pi executable is not installed"
        return None

    def launch_interactive(self, project_root: Path, **kwargs) -> int:
        """Interactive pi session via the gate-hooked `pb-pi` wrapper.

        Delegates to the shipped wrapper so the programmatic launch path gets the
        SAME hook adapter (`-e`), model allow-list, and config isolation as the
        CLI path — `detect_capabilities()` advertises hooks, so launching hookless
        pi here would be a footgun (impl-review T145 #2). Falls back to direct
        `pi` is never an acceptable fallback because it omits the required
        identity/policy extension.
        """
        from provider.session_identity import scrub_inherited_session_identity
        env = scrub_inherited_session_identity(os.environ)
        wrapper = self._wrapper_path()
        unsupported_reason = self._interactive_support_error()
        if unsupported_reason:
            raise RuntimeError(f"interactive pi unsupported: {unsupported_reason}")
        assert wrapper is not None
        env["PLAYBOOK_PROVIDER"] = "pi"
        result = subprocess.run(
            [str(wrapper)], cwd=str(project_root), env=env, **kwargs
        )
        return result.returncode

    def interactive_argv(self, *, prompt: str, model: Optional[str] = None,
                         resume_session_id: Optional[str] = None) -> list[str]:
        # pb-pi consumes an optional positional model; `--` then forwards the
        # provider arguments without misreading the prompt as a model.
        argv = [model] if model else []
        argv.append("--")
        if resume_session_id:
            argv += ["--session", resume_session_id]
        return [*argv, prompt]

    def launch_headless(self, project_root: Path, prompt: str, **kwargs) -> str:
        """Headless one-shot via sandbox."""
        from provider import sandbox as _sandbox
        result = _sandbox.run(
            "pi", ["-p", prompt, "--provider", "oss", "--no-context-files"],
            project_root=project_root,
            capture_output=True, text=True, **kwargs,
        )
        return result.stdout or ""

    # ── Capabilities ────────────────────────────────────────────────────────

    def detect_capabilities(self) -> ProviderCapabilities:
        supported = self.session_conformance().supported
        return ProviderCapabilities(
            provider="pi",
            # pi's extension system (`pi -e`) bridges to the bash hooks — see the
            # Hooks section + T145. tool_call→PreToolUse (real `{block}` deny),
            # tool_result→PostToolUse (gate echo), input→UserPromptSubmit.
            has_user_prompt_hook=supported,
            has_pre_tool_hook=supported,
            has_post_tool_hook=supported,
            # No blocking Stop equivalent: pi can't refuse to end a turn, only
            # soft-renudge via sendUserMessage. Anti-walk-away enforcement is
            # deferred (Phase 2). Keep False so callers don't assume hard Stop.
            has_stop_hook=False,
            # The extension reads ctx.sessionManager.getSessionId() and injects
            # that native ID into every hook payload and command environment.
            session_id_in_payload=supported,
            session_log_format="none",
            session_log_base=None,
        )

    def session_conformance(self) -> SessionConformance:
        unsupported_reason = self._interactive_support_error()
        supported = unsupported_reason is None
        return declared_session_conformance(
            "pi",
            exact_resume=True,
            resume_cwd="current project with pb-pi --session <native-id>",
            supported=supported,
            unsupported_reason=unsupported_reason,
        )

    # ── Session log / chat log ──────────────────────────────────────────────
    # pi has no on-disk session log we can parse — at least not in v0.73.0.
    # When upstream ships a session log, wire it here.

    def session_log_path(self) -> Optional[Path]:
        return None

    def read_new_messages(self, since_offset: int) -> tuple[list[str], int]:
        return [], since_offset
