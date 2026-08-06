"""
ClaudeAdapter — reference implementation of ProviderAdapter for Claude Code.

This is the adapter every other provider is measured against. Claude Code
provides the richest hook surface: all five capability flags are True.

Session identity: session_id is injected by Claude Code into every hook's
stdin payload under the key "session_id". No PID-walk or SQLite needed.

Chat log capture: reads session JSONL at
    ~/.claude/projects/<project-slug>/<session_id>.jsonl
incrementally by byte offset. Does NOT parse from hook stdin — file-based
capture is provider-portable and survives stdin truncation.

Integration: spec-only in T111. The existing bash hooks (task-gate-hook,
chat-log-hook, stop-hook, state-echo-hook) are the authoritative
implementation. This adapter documents the intended Python interface for T112.
"""

from __future__ import annotations
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from ..adapter import ProviderAdapter, Invocation
from ..capabilities import ProviderCapabilities, SessionFacts
from ..events import MessageEvent, ToolEvent, StopEvent
from ..policy import Decision


class ClaudeAdapter(ProviderAdapter):
    """Reference provider adapter for Claude Code (claude CLI)."""

    # Noise patterns to skip when reading session JSONL
    _NOISE_PREFIXES = (
        "<command-name>",
        "<task-notification>",
        "<system-reminder>",
        "[Request interrupted by user]",
    )

    # Variant label → --model CLI argument
    _MODEL_MAP = {
        "opus-5": "claude-opus-5",
        "sonnet-5": "claude-sonnet-5",
        "haiku-4-5": "claude-haiku-4-5",
    }
    # NOTE: claude-fable-5 deliberately NOT listed — Fable 5 / Mythos 5 were
    # pulled offline 2026-06-12 by US Commerce export-control order (disabled
    # worldwide). Re-add as a named-only variant if/when it returns.
    # Opus 5 / Sonnet 5 shipped 2026-06-30 (T147 refresh); Haiku still 4.5
    # (no Haiku 5 yet). Dropped the opus [1m] suffix — unconfirmed on Opus 5.

    # Variants in the default panel fan-out (claude isn't in the shipped panel
    # default anyway — see models.json "panel"; this backs the legacy fan-out
    # fallback and bare-"claude" naming).
    _DEFAULT_PANEL_VARIANTS = ("opus-5", "sonnet-5", "haiku-4-5")

    def __init__(self, session_id: str, project_root: Path) -> None:
        self._session_id = session_id
        self._project_root = project_root

    # ── CLI identity ─────────────────────────────────────────────────────────

    @classmethod
    def binary_name(cls) -> str:
        return "claude"

    @classmethod
    def panel_variants(cls) -> list[Optional[str]]:
        return list(cls._DEFAULT_PANEL_VARIANTS)

    def headless_argv(
        self,
        prompt: str,
        model: Optional[str],
        *,
        context: str = "",
        bare: bool = False,
        stream: bool = False,
    ) -> Invocation:
        # Bypass flag injected by provider.sandbox.run() — don't pass here.
        model_arg = self._MODEL_MAP.get(model, model) if model else "sonnet"
        argv = ["-p", prompt, "--model", model_arg]
        if context and not bare:
            argv += ["--append-system-prompt", context]
        if stream:
            argv += ["--output-format", "stream-json", "--include-partial-messages"]
        return Invocation(argv)

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
        tools = "Read,Glob,Grep"
        if web_search:
            tools += ",WebSearch"
        env = os.environ.copy()
        env["CLAUDECODE"] = ""
        env.pop("CLAUDE_CODE_SSE_PORT", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        env.pop("CLAUDE_PROJECT_DIR", None)
        env["PLAYBOOK_SESSION_ID"] = self._session_id or "judge"
        inv = self.headless_argv(prompt, model, context=system_context)
        # Judge-only extras layered on the core invocation.
        agent_args = inv.argv + [
            "--max-budget-usd", "2",
            "--tools", tools,
            "--allowedTools", tools,
        ]
        from provider import sandbox as _sandbox
        result = _sandbox.run(
            "claude", agent_args,
            project_root=self._project_root,
            env=env,
            capture_output=True, text=True, timeout=timeout_secs,
        )
        return _sandbox.format_judge_output(result)

    # ── Identity ─────────────────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def project_root(self) -> Path:
        return self._project_root

    # ── Bootstrap ────────────────────────────────────────────────────────────

    def bootstrap_file_name(self) -> str:
        return "CLAUDE.md"

    def install_bootstrap(self, project_root: Path) -> None:
        """No-op: CLAUDE.md is managed by `pb-tasks init` separately."""

    # ── Hooks ─────────────────────────────────────────────────────────────────

    def install_hooks(self, project_root: Path) -> None:
        """No-op: `pb-tasks init` reconciles `.claude/settings.local.json`."""

    def uninstall_hooks(self, project_root: Path) -> None:
        """No-op: project reconciliation owns standalone Claude hook entries."""

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def launch_interactive(self, project_root: Path, **kwargs) -> int:
        """Launch `claude` with PLAYBOOK_SESSION_ID pre-set."""
        import uuid
        env = os.environ.copy()
        env["PLAYBOOK_SESSION_ID"] = self._session_id or str(uuid.uuid4())
        env["PLAYBOOK_PROJECT_ROOT"] = str(project_root)
        result = subprocess.run(["claude"], cwd=project_root, env=env, **kwargs)
        return result.returncode

    def launch_headless(self, project_root: Path, prompt: str, **kwargs) -> str:
        """Run `claude --print` for a single non-interactive prompt."""
        import uuid
        env = os.environ.copy()
        env["PLAYBOOK_SESSION_ID"] = self._session_id or str(uuid.uuid4())
        env["PLAYBOOK_PROJECT_ROOT"] = str(project_root)
        result = subprocess.run(
            ["claude", "--print", prompt],
            cwd=project_root, env=env, capture_output=True, text=True, **kwargs
        )
        return result.stdout

    # ── Capabilities ─────────────────────────────────────────────────────────

    def detect_capabilities(self) -> ProviderCapabilities:
        """All Claude Code capabilities are True. Validate session log base exists."""
        log_base = self._session_log_base()
        return ProviderCapabilities(
            provider="claude",
            has_user_prompt_hook=True,
            has_pre_tool_hook=True,
            has_post_tool_hook=True,
            has_stop_hook=True,
            session_id_in_payload=True,
            session_log_format="jsonl",
            session_log_base=log_base if log_base and log_base.exists() else None,
        )

    # ── Chat log ─────────────────────────────────────────────────────────────

    def session_log_path(self) -> Optional[Path]:
        """~/.claude/projects/<slug>/<session_id>.jsonl"""
        base = self._session_log_base()
        if base is None:
            return None
        path = base / f"{self._session_id}.jsonl"
        return path if path.exists() else None

    def read_new_messages(self, since_offset: int) -> tuple[list[str], int]:
        """Read user messages from session JSONL since byte offset.

        Filters:
        - isMeta=True records (local command output injections)
        - content starting with noise prefixes (slash commands, task-notifications)
        - empty content after stripping
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

                    if obj.get("type") != "user":
                        continue
                    if obj.get("isMeta"):
                        continue

                    content = obj.get("message", {}).get("content", "")
                    if isinstance(content, list):
                        parts = [
                            c.get("text", "")
                            for c in content
                            if isinstance(c, dict) and c.get("type") == "text"
                        ]
                        text = " ".join(parts).strip()
                    else:
                        text = str(content).strip()

                    if not text:
                        continue
                    if any(text.startswith(p) for p in self._NOISE_PREFIXES):
                        continue

                    messages.append(text)
        except OSError:
            pass

        return messages, new_offset

    # ── Internal ─────────────────────────────────────────────────────────────

    def _session_log_base(self) -> Optional[Path]:
        """Compute ~/.claude/projects/<slug>/ from project root."""
        slug = str(self._project_root).replace("/", "-")
        base = Path.home() / ".claude" / "projects" / slug
        return base

    @classmethod
    def from_hook_stdin(cls, stdin_json: dict, project_root: Path) -> "ClaudeAdapter":
        """Construct adapter from a hook's parsed stdin payload.

        Usage in hook scripts (once wired in T112):
            import json, sys
            payload = json.load(sys.stdin)
            adapter = ClaudeAdapter.from_hook_stdin(payload, find_project_root())
        """
        session_id = stdin_json.get("session_id", "default")
        return cls(session_id=session_id, project_root=project_root)
