"""
CodexAdapter — provider adapter for OpenAI Codex CLI.

Current Codex reality:
  - AGENTS.md is auto-loaded as repo guidance.
  - An experimental lifecycle hook system exists behind
    [features] hooks = true in ~/.codex/config.toml.
  - Repo-local .codex/hooks.json can register UserPromptSubmit / Stop hooks,
    while PreToolUse/PostToolUse currently emit only Bash events.

Playbook uses both surfaces:
  - AGENTS.md for default workflow teaching.
  - Optional hook installation for stronger stop-time steering.

Session identity: no CODEX_SESSION_ID env var. Resolve via:
  1. $PLAYBOOK_SESSION_ID (set by bin/playbook-codex wrapper — preferred)
  2. SQLite: ~/.codex/state_5.sqlite WHERE cwd=$PROJECT_ROOT ORDER BY updated_at DESC
  3. PID-walk fallback: find 'codex' in parent process chain, use pid-<N>

Chat log: reads ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
  - session_meta record at top of file has id and cwd for verification
  - user messages are response_item records with payload.role=="user"
  - filter AGENTS.md injections (long content or "# AGENTS" / "You are" prefix)
"""

from __future__ import annotations
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from ..adapter import ProviderAdapter, Invocation
from ..capabilities import ProviderCapabilities, SessionFacts
from ..codex_hooks import codex_config_path, enable_codex_hooks_feature, install_project_hooks
from ..policy import Decision

# Threshold for filtering AGENTS.md injections (very long "user" messages)
_AGENTS_MD_LENGTH_THRESHOLD = 2000
_AGENTS_MD_PREFIXES = ("# AGENTS", "You are ", "# Playbook")


class CodexAdapter(ProviderAdapter):
    """Provider adapter for Codex CLI (OpenAI)."""

    _PANEL_VARIANTS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]

    def __init__(self, session_id: str, project_root: Path) -> None:
        self._session_id = session_id
        self._project_root = project_root
        self._rollout_path: Optional[Path] = None  # cached after first SQLite lookup

    # ── CLI identity ─────────────────────────────────────────────────────────

    @classmethod
    def binary_name(cls) -> str:
        return "codex"

    @classmethod
    def panel_variants(cls) -> list[Optional[str]]:
        return list(cls._PANEL_VARIANTS)

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
        if not shutil.which(self.binary_name()):
            return f"(error: {self.binary_name()} not found on PATH)"
        inv = self.headless_argv(prompt, model, context=system_context)
        # Judge-only extra: web search prepended before `exec`.
        agent_args = (["--search"] if web_search else []) + inv.argv
        env = os.environ.copy()
        env["PLAYBOOK_SESSION_ID"] = self._session_id or "judge"
        from provider import sandbox as _sandbox
        result = _sandbox.run(
            "codex", agent_args,
            project_root=self._project_root,
            env=env,
            input=inv.stdin, capture_output=True, text=True,
            timeout=timeout_secs,
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
        # codex reads its prompt from stdin (argv ends in "-"); context is
        # joined into the stdin payload. Bypass flag inserted after `exec` by
        # provider.sandbox._compose_agent_argv; outer seatbelt/bwrap provides
        # write containment (codex's internal --sandbox would nest and fail).
        full_prompt = prompt if (bare or not context) else f"{context}\n\n---\n\n{prompt}"
        argv = ["exec", "--ephemeral", "--skip-git-repo-check", "-s", "workspace-write"]
        if model:
            argv += ["-m", model]
        argv.append("-")
        return Invocation(argv, stdin=full_prompt)

    # ── Identity ─────────────────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def project_root(self) -> Path:
        return self._project_root

    # ── Bootstrap ────────────────────────────────────────────────────────────

    def bootstrap_file_name(self) -> str:
        return "AGENTS.md"

    def install_bootstrap(self, project_root: Path) -> None:
        """Write AGENTS.md teaching Codex the Playbook workflow.

        AGENTS.md is auto-loaded by Codex (baked into its base_instructions).
        Does not overwrite an existing AGENTS.md.  To refresh after a Playbook
        upgrade, delete AGENTS.md first, then re-run `pb-tasks init --provider codex`.
        """
        from tasks.template import agents_md_template
        target = project_root / "AGENTS.md"
        if not target.exists():
            target.write_text(agents_md_template(), encoding="utf-8")

    # ── Hooks ─────────────────────────────────────────────────────────────────

    def install_hooks(self, project_root: Path) -> None:
        """Enable Codex hooks globally and install repo-local Playbook hooks.

        Global enablement lives in ~/.codex/config.toml (`features.hooks`).
        Actual Playbook behavior is repo-local via <project>/.codex/hooks.json.
        """
        config_changed = enable_codex_hooks_feature(codex_config_path())
        hooks_path = install_project_hooks(project_root)
        print("  Codex hooks     installed")
        print("    Hooks file:   .codex/hooks.json")
        if config_changed:
            print("    Global config: enabled [features].hooks in ~/.codex/config.toml")
        else:
            print("    Global config: [features].hooks already enabled")
        print("    Warning: this Codex feature flag is global; the installed hooks file is repo-local")

    def uninstall_hooks(self, project_root: Path) -> None:
        """No user-facing uninstall flow yet; leave global config unchanged."""

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def launch_interactive(self, project_root: Path, **kwargs) -> int:
        """Launch `codex` TUI with PLAYBOOK_SESSION_ID pre-set.

        For the user-facing entry point (shell script with project-root
        discovery), use `bin/playbook-codex` instead.  This method is for
        programmatic use where the caller already knows the project root.
        """
        import uuid
        env = os.environ.copy()
        env["PLAYBOOK_SESSION_ID"] = self._session_id or str(uuid.uuid4())
        env["PLAYBOOK_PROJECT_ROOT"] = str(project_root)
        result = subprocess.run(["codex"], cwd=project_root, env=env, **kwargs)
        return result.returncode

    def launch_headless(self, project_root: Path, prompt: str, **kwargs) -> str:
        """Run `codex exec` for a single non-interactive prompt."""
        import uuid
        env = os.environ.copy()
        env["PLAYBOOK_SESSION_ID"] = self._session_id or str(uuid.uuid4())
        env["PLAYBOOK_PROJECT_ROOT"] = str(project_root)
        result = subprocess.run(
            ["codex", "exec", prompt],
            cwd=project_root, env=env, capture_output=True, text=True, **kwargs
        )
        return result.stdout

    # ── Capabilities ─────────────────────────────────────────────────────────

    def detect_capabilities(self) -> ProviderCapabilities:
        """Probe Codex environment for available capabilities.

        When hooks=true in ~/.codex/config.toml, the experimental hook
        system is active: UserPromptSubmit, PreToolUse (Bash only), PostToolUse
        (Bash only), and Stop all fire at turn scope and support block+reason.

        session_id_in_payload: False — not observed in hook payloads.
        """
        hooks_enabled = self._probe_stop_hook() and self._has_playbook_hooks()
        log_base = Path.home() / ".codex" / "sessions"
        return ProviderCapabilities(
            provider="codex",
            has_user_prompt_hook=hooks_enabled,
            has_pre_tool_hook=hooks_enabled,   # Bash-only when enabled
            has_post_tool_hook=hooks_enabled,  # Bash-only when enabled
            has_stop_hook=hooks_enabled,
            session_id_in_payload=False,
            session_log_format="jsonl",
            session_log_base=log_base if log_base.exists() else None,
        )

    def _probe_stop_hook(self, config_path: Optional[Path] = None) -> bool:
        """Check ~/.codex/config.toml for [features] hooks = true.

        Codex exposes its hook system behind a feature flag. The flag was renamed
        `codex_hooks` -> `hooks` (stable as of codex 0.141); we accept either so a
        config that hasn't been re-migrated still reads as enabled.
        When enabled, Stop/PreToolUse/PostToolUse hooks fire at turn scope and can
        return {"decision": "block", "reason": "..."} to continue the turn with a
        steering prompt.

        The config_path parameter is exposed for testing (defaults to the real path).
        """
        if config_path is None:
            config_path = Path.home() / ".codex" / "config.toml"
        if not config_path.exists():
            return False
        try:
            import tomllib
            with open(config_path, "rb") as f:
                config = tomllib.load(f)
            features = config.get("features", {})
            return (features.get("hooks", False) is True
                    or features.get("codex_hooks", False) is True)
        except Exception:
            return False

    def _has_playbook_hooks(self, hooks_path: Optional[Path] = None) -> bool:
        """Return True when repo-local .codex/hooks.json contains Playbook hooks."""
        if hooks_path is None:
            hooks_path = self._project_root / ".codex" / "hooks.json"
        if not hooks_path.exists():
            return False
        try:
            hooks_doc = json.loads(hooks_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False

        commands = {
            hook.get("command", "")
            for entries in hooks_doc.get("hooks", {}).values()
            for entry in entries
            for hook in entry.get("hooks", [])
            if isinstance(hook, dict)
        }
        return any("codex-user-prompt-hook" in command for command in commands) and any(
            "codex-stop-hook" in command for command in commands
        )

    # ── Chat log ─────────────────────────────────────────────────────────────

    def session_log_path(self) -> Optional[Path]:
        """Resolve session JSONL via SQLite state_5.sqlite.

        Queries: WHERE cwd = project_root ORDER BY updated_at DESC LIMIT 1
        Double-verifies by reading session_meta.cwd from top of JSONL.
        Returns None if SQLite unavailable or no matching thread found.
        """
        if self._rollout_path is not None:
            return self._rollout_path

        sqlite_path = Path.home() / ".codex" / "state_5.sqlite"
        if not sqlite_path.exists():
            return None

        try:
            import sqlite3
            cwd = str(self._project_root)
            with sqlite3.connect(sqlite_path) as conn:
                row = conn.execute(
                    "SELECT rollout_path FROM threads WHERE cwd = ? ORDER BY updated_at DESC LIMIT 1",
                    (cwd,)
                ).fetchone()
            if not row:
                return None

            rollout_path = Path(row[0])
            if not rollout_path.exists():
                return None

            # Double-verify: check session_meta.cwd matches our project root
            if not self._verify_session_meta_cwd(rollout_path, cwd):
                return None

            self._rollout_path = rollout_path
            return rollout_path
        except Exception:
            return None

    def _verify_session_meta_cwd(self, rollout_path: Path, expected_cwd: str) -> bool:
        """Read first session_meta record and verify cwd matches."""
        try:
            with open(rollout_path, "rb") as f:
                for raw_line in f:
                    obj = json.loads(raw_line.decode("utf-8", errors="replace"))
                    if obj.get("type") == "session_meta":
                        return obj.get("payload", {}).get("cwd") == expected_cwd
        except (OSError, json.JSONDecodeError):
            pass
        return False

    def read_new_messages(self, since_offset: int) -> tuple[list[str], int]:
        """Read user messages from Codex session JSONL since byte offset.

        User messages: type=response_item, payload.role==user.
        Filters: AGENTS.md injections (very long content or known prefixes).
        """
        log_path = self.session_log_path()
        if log_path is None:
            return [], since_offset

        messages: list[str] = []
        new_offset = since_offset

        try:
            with open(log_path, "rb") as f:
                f.seek(since_offset)
                for raw_line in f:
                    new_offset += len(raw_line)
                    try:
                        obj = json.loads(raw_line.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        continue

                    if obj.get("type") != "response_item":
                        continue
                    payload = obj.get("payload", {})
                    if payload.get("role") != "user":
                        continue

                    content = payload.get("content", [])
                    if isinstance(content, list):
                        parts = [
                            c.get("text", "")
                            for c in content
                            if isinstance(c, dict) and c.get("type") == "input_text"
                        ]
                        text = " ".join(parts).strip()
                    else:
                        text = str(content).strip()

                    if not text:
                        continue
                    # Filter AGENTS.md injections
                    if len(text) > _AGENTS_MD_LENGTH_THRESHOLD:
                        continue
                    if any(text.startswith(p) for p in _AGENTS_MD_PREFIXES):
                        continue

                    messages.append(text)
        except OSError:
            pass

        return messages, new_offset

    # ── Class method ─────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls, project_root: Path) -> "CodexAdapter":
        """Construct adapter using best available session ID source.

        Priority:
        1. PLAYBOOK_SESSION_ID (set by bin/playbook-codex wrapper)
        2. PID-walk to find 'codex' parent process
        3. SQLite-derived rollout UUID (resolved lazily in session_log_path)
        """
        session_id = os.environ.get("PLAYBOOK_SESSION_ID", "")
        if not session_id:
            session_id = _pid_walk_session_id(provider_names=["codex"])
        return cls(session_id=session_id, project_root=project_root)


def _pid_walk_session_id(provider_names: list[str]) -> str:
    """Walk PID ancestry to find a provider process; return pid-<N>.

    Falls back to "default" if no provider process is found in ancestry.
    Used only for Codex/agy/pi — Claude provides session_id in stdin directly.
    """
    try:
        import os as _os
        pid = _os.getpid()
        for _ in range(10):  # max 10 hops up the tree
            try:
                result = subprocess.run(
                    ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                    capture_output=True, text=True, timeout=1
                )
                parts = result.stdout.strip().split(None, 1)
                if len(parts) < 2:
                    break
                ppid, comm = int(parts[0]), parts[1].strip()
                # Strip path prefix if present
                comm_name = comm.split("/")[-1]
                if comm_name in provider_names:
                    return f"pid-{pid}"
                if ppid <= 1:
                    break
                pid = ppid
            except (ValueError, subprocess.TimeoutExpired, OSError):
                break
    except Exception:
        pass
    return "default"
