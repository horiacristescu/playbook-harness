"""Phase-A provider adapter for wrapper-free OMP interactive enforcement."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from ..adapter import Invocation, ProviderAdapter
from ..capabilities import ProviderCapabilities
from ..session_identity import SessionConformance, declared_session_conformance


_MANAGED_BY = "playbook-harness"
_LEGACY_MANAGED_BY = "claude-playbook"
_SCHEMA = 1
_BRIDGE_MARKER = "// playbook-managed: playbook-harness omp bridge schema=1"
_LEGACY_BRIDGE_MARKER = "// playbook-managed: claude-playbook omp bridge schema=1"
_PROVIDER_SOURCE = 'const EMBEDDED_PROVIDER: "omp" | undefined = undefined;'
_PROVIDER_OMP = 'const EMBEDDED_PROVIDER: "omp" | undefined = "omp";'


class OmpAdapter(ProviderAdapter):
    """Install the shared Pi-runtime bridge for bare, project-root OMP use.

    This adapter intentionally implements only Phase A. It is instantiable for
    ``pb-tasks init --provider omp`` and interactive launch, but is not
    registered as a judge, sandbox agent, or subagent backend.
    """

    def __init__(self, session_id: str, project_root: Path) -> None:
        self._session_id = session_id
        self._project_root = project_root

    @classmethod
    def binary_name(cls) -> str:
        return "omp"

    @classmethod
    def panel_variants(cls) -> list[Optional[str]]:
        return []

    def headless_argv(
        self,
        prompt: str,
        model: Optional[str],
        *,
        context: str = "",
        bare: bool = False,
        stream: bool = False,
    ) -> Invocation:
        raise NotImplementedError("OMP headless integration is deferred to Phase B")

    def run_headless_judge(
        self,
        prompt: str,
        model: Optional[str],
        system_context: str,
        *,
        web_search: bool,
        timeout_secs: int,
    ) -> str:
        raise NotImplementedError("OMP judge integration is deferred to Phase B")

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def project_root(self) -> Path:
        return self._project_root

    def bootstrap_file_name(self) -> str:
        return "AGENTS.md"

    def install_bootstrap(self, project_root: Path) -> None:
        from tasks.template import agents_md_template

        target = project_root / "AGENTS.md"
        if not target.exists():
            target.write_text(agents_md_template(), encoding="utf-8")

    @staticmethod
    def _shipped_bridge() -> Optional[Path]:
        here = Path(__file__).resolve()
        for parent in here.parents:
            candidate = parent / "scripts" / "playbook-pi-hook-adapter.ts"
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _paths(project_root: Path) -> tuple[Path, Path]:
        return (
            project_root / ".omp" / "extensions" / "playbook.ts",
            project_root / ".omp" / "playbook.json",
        )

    @staticmethod
    def _owned_bridge(path: Path) -> bool:
        if not path.is_file() or path.is_symlink():
            return False
        try:
            text = path.read_text(encoding="utf-8")
            first = text.splitlines()[0]
            if first in {_BRIDGE_MARKER, _LEGACY_BRIDGE_MARKER}:
                return True
            from tasks.reconcile import MANAGED_BY, MANAGED_SCHEMA, parse_managed_file

            parsed = parse_managed_file(text, "slash")
            if parsed is None:
                return False
            metadata, body = parsed
            return (
                metadata.get("managed_by") == MANAGED_BY
                and metadata.get("schema") == MANAGED_SCHEMA
                and metadata.get("source") == "scripts/playbook-pi-hook-adapter.ts"
                and metadata.get("source_hash")
                == hashlib.sha256(body.encode("utf-8")).hexdigest()
            )
        except (OSError, ValueError):
            return False

    @staticmethod
    def _owned_metadata(path: Path) -> bool:
        if not path.is_file() or path.is_symlink():
            return False
        try:
            text = path.read_text(encoding="utf-8")
            value = json.loads(text)
            managed = value.get("_playbook_harness")
            if managed is not None:
                from tasks.reconcile import (
                    MANAGED_BY,
                    MANAGED_SCHEMA,
                    json_value_digest,
                    parse_managed_file,
                )

                parsed = parse_managed_file(text, "json")
                if parsed is None:
                    return False
                marker, body = parsed
                return (
                    marker.get("managed_by") == MANAGED_BY
                    and marker.get("schema") == MANAGED_SCHEMA
                    and marker.get("source")
                    == "tasks/provider_contributions:omp-metadata"
                    and marker.get("source_hash")
                    == json_value_digest(json.loads(body))
                    and json.loads(body).get("provider") == "omp"
                )
        except (OSError, json.JSONDecodeError, ValueError):
            return False
        return (
            value.get("managed_by") in {_MANAGED_BY, _LEGACY_MANAGED_BY}
            and value.get("schema") == _SCHEMA
            and value.get("provider") == "omp"
        )

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def install_hooks(self, project_root: Path) -> None:
        bridge_source = self._shipped_bridge()
        if bridge_source is None:
            raise RuntimeError("shipped playbook-pi-hook-adapter.ts not found")
        extension, metadata = self._paths(project_root)

        collisions = []
        if (extension.exists() or extension.is_symlink()) and not self._owned_bridge(extension):
            collisions.append(extension)
        if (metadata.exists() or metadata.is_symlink()) and not self._owned_metadata(metadata):
            collisions.append(metadata)
        if collisions:
            joined = ", ".join(str(path) for path in collisions)
            raise RuntimeError(f"refusing to overwrite non-Playbook OMP artifact(s): {joined}")

        bridge_body = bridge_source.read_text(encoding="utf-8")
        if bridge_body.count(_PROVIDER_SOURCE) != 1:
            raise RuntimeError("shared bridge embedded-provider marker missing or ambiguous")
        bridge_body = bridge_body.replace(_PROVIDER_SOURCE, _PROVIDER_OMP)
        bridge_text = _BRIDGE_MARKER + "\n" + bridge_body
        metadata_text = json.dumps(
            {
                "managed_by": _MANAGED_BY,
                "schema": _SCHEMA,
                "provider": "omp",
                "hook_dir": str(bridge_source.parent.resolve()),
            },
            indent=2,
            sort_keys=True,
        ) + "\n"
        self._atomic_write(extension, bridge_text)
        self._atomic_write(metadata, metadata_text)

    def uninstall_hooks(self, project_root: Path) -> None:
        extension, metadata = self._paths(project_root)
        if self._owned_bridge(extension):
            extension.unlink()
        if self._owned_metadata(metadata):
            metadata.unlink()
        for directory in (extension.parent, extension.parent.parent):
            try:
                directory.rmdir()
            except OSError:
                pass

    def launch_interactive(self, project_root: Path, **kwargs) -> int:
        from provider.session_identity import scrub_inherited_session_identity
        env = scrub_inherited_session_identity(os.environ)
        env["PLAYBOOK_PROVIDER"] = "omp"
        result = subprocess.run(["omp"], cwd=str(project_root), env=env, **kwargs)
        return result.returncode

    def interactive_argv(self, *, prompt: str, model: Optional[str] = None,
                         resume_session_id: Optional[str] = None) -> list[str]:
        argv: list[str] = []
        if resume_session_id:
            argv.append(f"--resume={resume_session_id}")
        if model:
            argv += ["--model", model]
        return [*argv, prompt]

    def launch_headless(self, project_root: Path, prompt: str, **kwargs) -> str:
        raise NotImplementedError("OMP headless integration is deferred to Phase B")

    def detect_capabilities(self) -> ProviderCapabilities:
        supported = self.session_conformance().supported
        return ProviderCapabilities(
            provider="omp",
            has_user_prompt_hook=supported,
            has_pre_tool_hook=supported,
            has_post_tool_hook=supported,
            has_stop_hook=False,
            session_id_in_payload=supported,
            session_log_format="none",
            session_log_base=None,
        )

    def session_conformance(self) -> SessionConformance:
        extension, metadata = self._paths(self._project_root)
        supported = self._owned_bridge(extension) and self._owned_metadata(metadata)
        return declared_session_conformance(
            "omp",
            exact_resume=True,
            resume_cwd="current project with omp --resume <native-id>",
            supported=supported,
            unsupported_reason=(
                None
                if supported
                else "managed project OMP identity bridge is not installed"
            ),
        )

    def session_log_path(self) -> Optional[Path]:
        return None

    def read_new_messages(self, since_offset: int) -> tuple[list[str], int]:
        return [], since_offset
