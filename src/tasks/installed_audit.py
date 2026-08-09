"""Self-contained integrity audit for an installed public checkout."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

ARTIFACT_MANIFEST = ".playbook-artifact.json"
MANIFEST_SCHEMA = 1
REQUIRED_PATHS = frozenset({
    "install.sh", "README.md", "bin/pb-tasks", "bin/pb-sandbox",
    "bin/pb-codex", "bin/pb-agy", "bin/pb-pi", "bin/pb-tmux-agent",
    "hooks/claude-standalone.json", "scripts/playbook-pi-hook-adapter.ts",
    "scripts/playbook-pi-omlx-models.json", "src/tasks/cli.py",
    "src/tasks/installed_audit.py", "src/tmux_agent.py",
})


def _safe_relative(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        return None
    return path.as_posix()


def _manifest_records(value: Any, errors: list[str]) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or value.get("schema") != MANIFEST_SCHEMA:
        errors.append(f"artifact manifest schema must be {MANIFEST_SCHEMA}")
        return {}
    records = value.get("files")
    if not isinstance(records, list):
        errors.append("artifact manifest files must be a list")
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            errors.append(f"artifact manifest record {index} must be an object")
            continue
        destination = _safe_relative(record.get("destination"))
        source = _safe_relative(record.get("source"))
        digest = record.get("sha256")
        mode = record.get("mode")
        if destination is None or source is None:
            errors.append(f"artifact manifest record {index} has an unsafe path")
            continue
        if destination in indexed:
            errors.append(f"artifact manifest duplicate destination: {destination}")
            continue
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            errors.append(f"artifact manifest invalid hash: {destination}")
            continue
        if mode not in ("0644", "0755"):
            errors.append(f"artifact manifest invalid mode: {destination}")
            continue
        indexed[destination] = record
    return indexed


def _actual_files(root: Path, errors: list[str]) -> set[str]:
    files: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            errors.append(f"installed artifact symlink is forbidden: {relative.as_posix()}")
        elif path.is_file():
            files.add(relative.as_posix())
    return files


def _audit_hook_edges(root: Path, errors: list[str]) -> None:
    try:
        spec = json.loads((root / "hooks/claude-standalone.json").read_text(encoding="utf-8"))
        events = spec.get("events") if isinstance(spec, dict) else None
        if not isinstance(events, dict):
            raise ValueError("events must be an object")
        for entries in events.values():
            if not isinstance(entries, list):
                raise ValueError("event entries must be lists")
            for entry in entries:
                script = entry.get("script") if isinstance(entry, dict) else None
                if _safe_relative(script) != script or "/" in script:
                    raise ValueError("hook script must be one safe filename")
                if not (root / "scripts" / script).is_file():
                    errors.append(f"installed hook dependency missing: scripts/{script}")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"installed Claude hook spec is invalid: {exc}")


def _audit_python_edges(root: Path, errors: list[str]) -> None:
    source_root = root / "src"
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(source_root)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            errors.append(f"installed Python module is invalid: {relative.as_posix()}: {exc}")
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                if name.split(".", 1)[0] not in {"tasks", "provider"}:
                    continue
                module = source_root.joinpath(*name.split("."))
                if not module.with_suffix(".py").is_file() and not (module / "__init__.py").is_file():
                    errors.append(f"installed Python dependency missing: {name}")


def audit_installed_tree(root: Path) -> list[str]:
    """Validate one built candidate or Git clone using only its own manifest."""
    target = root.resolve()
    errors: list[str] = []
    if root.is_symlink() or not target.is_dir():
        return ["installed artifact root must be a real directory"]
    try:
        manifest = json.loads((target / ARTIFACT_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid artifact manifest: {exc}"]
    records = _manifest_records(manifest, errors)
    for relative in sorted(REQUIRED_PATHS - set(records)):
        errors.append(f"artifact manifest omits required runtime path: {relative}")
    expected = set(records) | {ARTIFACT_MANIFEST}
    actual = _actual_files(target, errors)
    for relative in sorted(expected - actual):
        errors.append(f"installed artifact file missing: {relative}")
    for relative in sorted(actual - expected):
        errors.append(f"installed artifact has unowned file: {relative}")
    for relative, record in sorted(records.items()):
        path = target / relative
        if not path.is_file() or path.is_symlink():
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            errors.append(f"installed artifact hash mismatch: {relative}")
        actual_mode = path.stat().st_mode & 0o777
        expected_mode = int(record["mode"], 8)
        if actual_mode != expected_mode:
            errors.append(f"installed artifact mode mismatch: {relative} expected {expected_mode:04o}, got {actual_mode:04o}")
    _audit_hook_edges(target, errors)
    _audit_python_edges(target, errors)
    return sorted(set(errors))
