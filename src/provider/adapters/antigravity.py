"""
AntigravityAdapter — provider adapter for Google's Antigravity CLI (`agy`).

agy v1.0.2 (Go-based, brew cask) replaces the legacy `gemini` binary
(sunsets 2026-06-18). It stores state under ~/.gemini/antigravity/ — bootstrap
file is GEMINI.md (auto-loaded by agy from project cwd, same convention as
~/.gemini/GEMINI.md at user scope).

Hook surface: agy v1.0.2 has a Claude-compatible plugin loader that accepts
PreToolUse / PostToolUse / UserPromptSubmit / Stop hooks via project-local
plugin manifests. install_hooks writes the manifest (T134 W5a).

Session identity: agy v1.1.10 exposes one native conversation ID as hook payload
`conversationId` and command environment `ANTIGRAVITY_CONVERSATION_ID`.

Session transcript: JSONL at
    ~/.gemini/antigravity/brain/<uuid>/.system_generated/logs/transcript.jsonl
Records of interest: source=USER_EXPLICIT, type=USER_INPUT — content wrapped
in <USER_REQUEST>...</USER_REQUEST>, optionally followed by <ADDITIONAL_METADATA>
and <USER_SETTINGS_CHANGE> blocks.

Panel-review participation: agy v1.1.9 accepts `--model`; the three configured
Gemini identifiers were probed live on 2026-08-05 before being added here.
"""

from __future__ import annotations
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from ..adapter import ProviderAdapter, Invocation
from ..capabilities import ProviderCapabilities, SessionFacts
from ..policy import Decision
from ..session_identity import (
    SessionConformance,
    command_session_id,
    declared_session_conformance,
    scrub_inherited_session_identity,
)


_USER_REQUEST_RE = re.compile(
    r"<USER_REQUEST>(.*?)</USER_REQUEST>",
    re.DOTALL,
)


class AntigravityAdapter(ProviderAdapter):
    """Provider adapter for Antigravity CLI (`agy`)."""

    _BRAIN_DIR = Path.home() / ".gemini" / "antigravity" / "brain"
    _PANEL_VARIANTS = [
        "gemini-3.6-flash-high",
        "gemini-3.5-flash-high",
        "gemini-3.1-pro-high",
    ]

    def __init__(self, session_id: str, project_root: Path) -> None:
        self._session_id = session_id
        self._project_root = project_root
        self._transcript_path: Optional[Path] = None  # cached after first lookup

    # ── CLI identity ─────────────────────────────────────────────────────────

    @classmethod
    def binary_name(cls) -> str:
        return "agy"

    @classmethod
    def panel_variants(cls) -> list[Optional[str]]:
        return list(cls._PANEL_VARIANTS)

    def headless_argv(
        self,
        prompt: str,
        model: Optional[str],
        *,
        context: str = "",
        bare: bool = False,
        stream: bool = False,
    ) -> Invocation:
        # Prompt is passed through --print (not stdin); --print mode ignores cwd,
        # so --add-dir exposes the project tree.
        # Bypass flag (--dangerously-skip-permissions) prepended by sandbox.
        full_prompt = prompt if (bare or not context) else f"{context}\n\n---\n\n{prompt}"
        argv = ["--add-dir", str(self._project_root)]
        if model:
            argv += ["--model", model]
        argv += ["--print", full_prompt]
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
        inv = self.headless_argv(prompt, model, context=system_context)
        # Judge-only extra: --print-timeout (Go-style duration).
        agent_args = inv.argv + ["--print-timeout", f"{timeout_secs}s"]
        env = os.environ.copy()
        env["PLAYBOOK_SESSION_ID"] = self._session_id or "judge"
        env["PLAYBOOK_ROLE"] = "noninteractive"
        from provider import sandbox as _sandbox
        result = _sandbox.run(
            "agy", agent_args,
            project_root=self._project_root,
            env=env,
            capture_output=True, text=True, timeout=timeout_secs + 30,
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
        return "GEMINI.md"

    def install_bootstrap(self, project_root: Path) -> None:
        """Write GEMINI.md teaching agy the Playbook workflow.

        agy auto-loads ~/.gemini/GEMINI.md at user scope; project-local
        GEMINI.md is read by agy when run from project cwd. Does not
        overwrite an existing GEMINI.md.
        """
        from tasks.template import antigravity_md_template
        target = project_root / "GEMINI.md"
        if not target.exists():
            target.write_text(antigravity_md_template(), encoding="utf-8")

    # ── Hooks ─────────────────────────────────────────────────────────────────

    _PLUGIN_NAME = "playbook-harness"
    _LEGACY_PLUGIN_NAMES = ("claude-playbook",)

    def install_hooks(self, project_root: Path) -> None:
        """Install Playbook hooks globally with agy via plugin manifest.

        agy plugins live in ~/.gemini/config/plugins/<name>/ and must be registered
        via `agy plugin install <src>` — direct file writes are not picked up.
        This method builds a cached manifest in the Harness XDG cache directory
        then invokes `agy plugin install` to register it globally. Idempotent —
        re-install if the plugin already exists (refreshes hook script paths).
        """
        import shutil
        agy_bin = shutil.which("agy")
        if not agy_bin:
            print("  agy plugin   skipped: 'agy' not on PATH")
            return

        scripts_dir = self._resolve_playbook_scripts_dir()
        if scripts_dir is None:
            print("  agy plugin   skipped: could not resolve Playbook scripts dir")
            return

        try:
            cache_dir = self._build_plugin_manifest(scripts_dir)
        except OSError as exc:
            print(f"  agy plugin   skipped: cannot create Harness cache ({exc})")
            return
        self._register_with_agy(agy_bin, cache_dir)

    def uninstall_hooks(self, project_root: Path) -> None:
        """Remove Playbook agy plugin registration."""
        import shutil
        agy_bin = shutil.which("agy")
        if not agy_bin:
            return
        for name in (self._PLUGIN_NAME, *self._LEGACY_PLUGIN_NAMES):
            subprocess.run(
                [agy_bin, "plugin", "uninstall", name],
                capture_output=True, text=True,
            )

    def _resolve_playbook_scripts_dir(self) -> Optional[Path]:
        """Locate the directory containing Playbook hook scripts.

        Walk up from this module to the canonical runtime scripts directory.
        """
        # Walk up from src/provider/adapters/antigravity.py
        here = Path(__file__).resolve()
        # adapters → provider → src → repo root
        for parent in here.parents:
            candidate = parent / "scripts"
            if (candidate / "task-gate-hook").exists():
                return candidate
        return None

    def _build_plugin_manifest(self, scripts_dir: Path) -> Path:
        """Write the agy plugin manifest under the Harness machine cache root.

        Returns the manifest root path suitable for `agy plugin install <path>`.
        """
        import shutil

        from tasks.core import VERSION
        from ..runtime_paths import user_cache_dir

        cache_dir = user_cache_dir() / "agy-plugin" / self._PLUGIN_NAME
        # This directory is the complete package input to `agy plugin install`.
        # Remove obsolete layouts before rebuilding: an older `hooks/hooks.json`
        # otherwise survives beside the current root `hooks.json`, and Agy can
        # execute both generations of hooks.
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "plugin.json").write_text(
            json.dumps({"name": self._PLUGIN_NAME, "version": VERSION}, indent=2),
            encoding="utf-8",
        )
        bridge = scripts_dir / "agy-hook-bridge.py"

        def _handler(event: str) -> dict:
            return {
                "type": "command",
                "command": f"python3 {shlex.quote(str(bridge))} {event}",
                "timeout": 5,
            }

        hooks_doc = {
            self._PLUGIN_NAME: {
                "PreInvocation": [_handler("pre-invocation")],
                "PreToolUse": [{"matcher": "*", "hooks": [_handler("pre-tool")]}],
                "PostToolUse": [{"matcher": "*", "hooks": [_handler("post-tool")]}],
                "Stop": [_handler("stop")],
            }
        }
        (cache_dir / "hooks.json").write_text(
            json.dumps(hooks_doc, indent=2), encoding="utf-8",
        )
        return cache_dir

    def _register_with_agy(self, agy_bin: str, cache_dir: Path) -> None:
        """Invoke `agy plugin install <cache_dir>`. Idempotent w.r.t. agy's state."""
        # Uninstall first to guarantee a refresh (script paths may have changed
        # since the previous install). Ignore errors — plugin may not exist yet.
        for name in (self._PLUGIN_NAME, *self._LEGACY_PLUGIN_NAMES):
            subprocess.run(
                [agy_bin, "plugin", "uninstall", name],
                capture_output=True, text=True,
            )
        result = subprocess.run(
            [agy_bin, "plugin", "install", str(cache_dir)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  agy plugin   install failed: {result.stderr.strip()}")
            return

        enabled = subprocess.run(
            [agy_bin, "plugin", "enable", self._PLUGIN_NAME],
            capture_output=True, text=True,
        )
        if enabled.returncode == 0:
            print(f"  agy plugin   installed and enabled ({self._PLUGIN_NAME})")
        else:
            print(f"  agy plugin   enable failed: {enabled.stderr.strip()}")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def interactive_argv(self, *, prompt: str, model: Optional[str] = None,
                         resume_session_id: Optional[str] = None) -> list[str]:
        argv: list[str] = []
        if resume_session_id:
            argv += ["--conversation", resume_session_id]
        if model:
            argv += ["--model", model]
        return [*argv, "--prompt-interactive", prompt]

    def launch_interactive(self, project_root: Path, **kwargs) -> int:
        """Launch `agy`; the provider creates and exposes its native ID."""
        env = scrub_inherited_session_identity(os.environ)
        env["PLAYBOOK_PROVIDER"] = "antigravity"
        env["PLAYBOOK_PROJECT_ROOT"] = str(project_root)
        result = subprocess.run(["agy"], cwd=project_root, env=env, **kwargs)
        return result.returncode

    def launch_headless(self, project_root: Path, prompt: str, **kwargs) -> str:
        """Run `agy --print` for a single non-interactive prompt.

        Uses --add-dir to expose the project tree — agy v1.0.2 --print mode
        runs in its own scratch dir and ignores cwd otherwise.
        """
        import uuid
        env = os.environ.copy()
        env["PLAYBOOK_SESSION_ID"] = self._session_id or str(uuid.uuid4())
        env["PLAYBOOK_ROLE"] = "noninteractive"
        env["PLAYBOOK_PROJECT_ROOT"] = str(project_root)
        result = subprocess.run(
            ["agy", "--add-dir", str(project_root),
             "--print", prompt, "--print-timeout", "300s"],
            cwd=project_root, env=env, capture_output=True, text=True, **kwargs,
        )
        return result.stdout

    # ── Capabilities ─────────────────────────────────────────────────────────

    def detect_capabilities(self) -> ProviderCapabilities:
        """Agy plugin hooks with provider-native camelCase payloads."""
        log_base = self._BRAIN_DIR
        return ProviderCapabilities(
            provider="antigravity",
            # Agy has PreInvocation but no UserPromptSubmit-equivalent payload.
            has_user_prompt_hook=False,
            has_pre_tool_hook=True,
            has_post_tool_hook=True,
            has_stop_hook=True,
            session_id_in_payload=True,
            session_log_format="jsonl",
            session_log_base=log_base if log_base.exists() else None,
        )

    # ── Chat log ─────────────────────────────────────────────────────────────

    def session_log_path(self) -> Optional[Path]:
        """Find most recent transcript JSONL referencing the project cwd.

        Walks ~/.gemini/antigravity/brain/<uuid>/.system_generated/logs/transcript.jsonl
        and returns the file with newest mtime whose content mentions project_root.
        Verification is content-based (cwd appears in early USER_INPUT or tool_calls)
        because agy doesn't tag transcripts with cwd metadata directly.
        """
        if self._transcript_path is not None:
            return self._transcript_path
        if not self._BRAIN_DIR.exists():
            return None
        cwd_str = str(self._project_root)
        candidates: list[tuple[float, Path]] = []
        for brain_dir in self._BRAIN_DIR.iterdir():
            transcript = brain_dir / ".system_generated" / "logs" / "transcript.jsonl"
            if transcript.exists():
                candidates.append((transcript.stat().st_mtime, transcript))
        candidates.sort(reverse=True)  # newest first
        for _, path in candidates:
            try:
                # Read first ~8KB to check for cwd reference
                with open(path, "rb") as f:
                    head = f.read(8192).decode("utf-8", errors="replace")
                if cwd_str in head:
                    self._transcript_path = path
                    return path
            except OSError:
                continue
        return None

    def read_new_messages(self, since_offset: int) -> tuple[list[str], int]:
        """Read user messages from agy transcript since byte offset.

        Filters: source=USER_EXPLICIT, type=USER_INPUT.
        Cleans: unwraps <USER_REQUEST>...</USER_REQUEST>; strips trailing
        <ADDITIONAL_METADATA> / <USER_SETTINGS_CHANGE> blocks.
        Returns ([], since_offset) if no transcript found.
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
                    if obj.get("source") != "USER_EXPLICIT":
                        continue
                    if obj.get("type") != "USER_INPUT":
                        continue
                    content = obj.get("content", "")
                    if not isinstance(content, str):
                        continue
                    # Prefer the explicit <USER_REQUEST> wrapper when present;
                    # fall back to raw content stripped of trailing metadata blocks.
                    m = _USER_REQUEST_RE.search(content)
                    if m:
                        text = m.group(1).strip()
                    else:
                        # Strip trailing <ADDITIONAL_METADATA>...</ADDITIONAL_METADATA>
                        # and <USER_SETTINGS_CHANGE>...</USER_SETTINGS_CHANGE> blocks.
                        text = re.sub(
                            r"<(ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>.*?</\1>",
                            "",
                            content,
                            flags=re.DOTALL,
                        ).strip()
                    if text:
                        messages.append(text)
        except OSError:
            pass

        return messages, new_offset

    # ── Class method ─────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls, project_root: Path) -> "AntigravityAdapter":
        """Construct from Agy's authoritative command environment only."""
        return cls(
            session_id=command_session_id("antigravity", os.environ),
            project_root=project_root,
        )

    def session_conformance(self) -> SessionConformance:
        """Publish the live-and-coded Agy 1.1.12 identity contract."""
        return declared_session_conformance(
            "antigravity",
            exact_resume=True,
            resume_cwd="current project with --conversation <native-id>",
            supported=True,
        )
