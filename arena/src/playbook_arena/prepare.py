"""Deterministic historical workspace materialization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from typing import Mapping

from .case import ArenaCaseError, CaseDefinition, safe_relative
from .git_source import list_source_tree, read_blob, reserve_staging, resolve_source


PROVENANCE_FILE = ".playbook-arena.json"
MAX_OVERLAY_FILES = 10_000
MAX_OVERLAY_FILE_BYTES = 64 * 1024 * 1024
MAX_OVERLAY_TOTAL_BYTES = 256 * 1024 * 1024
MAX_GIT_FILE_BYTES = 64 * 1024 * 1024
MAX_GIT_TOTAL_BYTES = 256 * 1024 * 1024
MAX_WORKSPACE_FILE_BYTES = 64 * 1024 * 1024
MAX_WORKSPACE_TOTAL_BYTES = 256 * 1024 * 1024
CREDENTIAL_BASENAMES = frozenset(
    {".env", ".npmrc", ".pypirc", "credentials.json", "id_rsa", "id_ed25519", "service-account.json"}
)
CREDENTIAL_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ArenaCaseError(f"cannot hash input {path}: {exc}") from exc
    return digest.hexdigest()


def _regular_files(root: Path, *, include_provenance: bool = False) -> list[Path]:
    files: list[Path] = []
    folded: set[str] = set()
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == PROVENANCE_FILE and not include_provenance:
            continue
        if path.is_symlink():
            raise ArenaCaseError(f"workspace links are forbidden: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ArenaCaseError(f"workspace special files are forbidden: {relative}")
        safe_relative(relative, label="workspace path")
        size = path.stat().st_size
        if size > MAX_WORKSPACE_FILE_BYTES:
            raise ArenaCaseError(f"workspace file is too large: {relative}")
        total += size
        if total > MAX_WORKSPACE_TOTAL_BYTES:
            raise ArenaCaseError("prepared workspace is too large")
        key = relative.casefold()
        if key in folded:
            raise ArenaCaseError(f"case-colliding workspace paths are forbidden: {relative}")
        folded.add(key)
        files.append(path)
    return files


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _regular_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        data = path.read_bytes()
        mode = b"0755" if path.stat().st_mode & 0o111 else b"0644"
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(mode)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _write_file(root: Path, relative: str, data: bytes, mode: int, *, collide: bool = False) -> None:
    safe = safe_relative(relative, label="output path")
    target = root.joinpath(*PurePosixPath(safe).parts)
    if target.exists() or target.is_symlink():
        if collide:
            raise ArenaCaseError(f"overlay path collides with prepared tree: {relative}")
        raise ArenaCaseError(f"duplicate output path: {relative}")
    target.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    target.write_bytes(data)
    target.chmod(0o755 if mode & 0o111 else 0o644)


def _verify_inputs(case: CaseDefinition, corpus: Path | None) -> Path | None:
    for relative, expected in case.input_hashes.items():
        if relative.startswith("overlay/"):
            continue
        path = case.root
        for part in PurePosixPath(relative).parts:
            path = path / part
            if path.is_symlink():
                raise ArenaCaseError(f"case input path may not traverse a symlink: {relative}")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ArenaCaseError(f"case input is unavailable: {relative}: {exc}") from exc
        if case.root not in resolved.parents or not resolved.is_file():
            raise ArenaCaseError(f"case input must be a real file: {relative}")
        if sha256_file(resolved) != expected:
            raise ArenaCaseError(f"case input hash mismatch: {relative}")
    if case.overlay is None:
        return None
    if corpus is None:
        raise ArenaCaseError("case requires a corpus; pass --corpus DIR")
    if corpus.is_symlink() or not corpus.is_dir():
        raise ArenaCaseError(f"corpus must be a real directory: {corpus}")
    overlay = corpus.resolve() / f"{case.overlay.sha256}.tar.gz"
    if overlay.is_symlink() or not overlay.is_file():
        raise ArenaCaseError(f"overlay corpus object is missing: {case.overlay.sha256}")
    if sha256_file(overlay) != case.overlay.sha256:
        raise ArenaCaseError(f"overlay corpus hash mismatch: {case.overlay.sha256}")
    return overlay


def _preflight_patch(data: bytes) -> None:
    if b"\0" in data:
        raise ArenaCaseError("patch contains NUL bytes")
    text = data.decode("utf-8", errors="strict")
    for line in text.splitlines():
        if line.startswith(("new file mode ", "old mode ", "new mode ")) and line.endswith(("120000", "160000")):
            raise ArenaCaseError("patch may not create links or submodules")
        if line.startswith("diff --git "):
            fields = line.split()
            if len(fields) != 4 or not fields[2].startswith("a/") or not fields[3].startswith("b/"):
                raise ArenaCaseError("patch has malformed diff paths")
            safe_relative(fields[2][2:], label="patch path")
            safe_relative(fields[3][2:], label="patch path")
        if line.startswith(("--- ", "+++ ")):
            raw = line[4:].split("\t", 1)[0]
            if raw == "/dev/null":
                continue
            if not raw.startswith(("a/", "b/")):
                raise ArenaCaseError("patch has unsafe file header")
            safe_relative(raw[2:], label="patch path")


def _apply_patch(case: CaseDefinition, staging: Path) -> None:
    if case.patch is None:
        return
    patch = case.root.joinpath(*PurePosixPath(case.patch).parts)
    data = patch.read_bytes()
    _preflight_patch(data)
    environment = os.environ.copy()
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1"})
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "apply", "--whitespace=nowarn", "--", str(patch)],
        cwd=staging,
        env=environment,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ArenaCaseError(f"preparation patch failed: {detail}")


def _extract_overlay(archive: Path, staging: Path) -> None:
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            if len(members) > MAX_OVERLAY_FILES:
                raise ArenaCaseError("overlay has too many members")
            total = 0
            seen: set[str] = set()
            planned: list[tuple[tarfile.TarInfo, str]] = []
            for member in members:
                relative = safe_relative(member.name.rstrip("/"), label="overlay path")
                folded = relative.casefold()
                if folded in seen:
                    raise ArenaCaseError(f"overlay has duplicate/case-colliding path: {relative}")
                seen.add(folded)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ArenaCaseError(f"overlay links/special files are forbidden: {relative}")
                if member.size > MAX_OVERLAY_FILE_BYTES:
                    raise ArenaCaseError(f"overlay member is too large: {relative}")
                total += member.size
                if total > MAX_OVERLAY_TOTAL_BYTES:
                    raise ArenaCaseError("overlay is too large")
                planned.append((member, relative))
            for member, relative in planned:
                extracted = bundle.extractfile(member)
                if extracted is None:
                    raise ArenaCaseError(f"cannot read overlay member: {relative}")
                _write_file(staging, relative, extracted.read(), member.mode, collide=True)
    except (OSError, tarfile.TarError, UnicodeError) as exc:
        raise ArenaCaseError(f"cannot read overlay: {exc}") from exc


def _scan_leakage(case: CaseDefinition, staging: Path) -> None:
    forbidden_content = [(value, value.encode("utf-8")) for value in case.forbidden_content]
    for path in _regular_files(staging):
        relative = path.relative_to(staging).as_posix()
        parts = PurePosixPath(relative).parts
        if ".git" in parts:
            raise ArenaCaseError(f"Git metadata is forbidden in prepared workspace: {relative}")
        for forbidden in case.forbidden_paths:
            if relative == forbidden or relative.startswith(f"{forbidden}/"):
                raise ArenaCaseError(f"forbidden future path is present: {forbidden}")
        if path.name in CREDENTIAL_BASENAMES or relative == ".aws/credentials":
            raise ArenaCaseError(f"credential-shaped path is forbidden: {relative}")
        data = path.read_bytes()
        for label, needle in forbidden_content:
            if needle in data:
                raise ArenaCaseError(f"forbidden future content is present: {label!r} in {relative}")
        for pattern in CREDENTIAL_PATTERNS:
            if pattern.search(data):
                raise ArenaCaseError(f"credential-shaped content is forbidden: {relative}")


def prepare_case(
    case: CaseDefinition,
    destination: str | Path,
    *,
    sources: Mapping[str, Path],
    corpus: Path | None = None,
) -> dict[str, object]:
    target = Path(destination).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    repository = resolve_source(case.source, sources)
    overlay = _verify_inputs(case, corpus)
    staging = reserve_staging(target)
    try:
        git_total = 0
        for entry in list_source_tree(repository, case.source):
            if entry.size > MAX_GIT_FILE_BYTES:
                raise ArenaCaseError(f"Git source file is too large: {entry.path}")
            git_total += entry.size
            if git_total > MAX_GIT_TOTAL_BYTES:
                raise ArenaCaseError("Git source subdirectory is too large")
            blob = read_blob(repository, entry.object_id)
            if len(blob) != entry.size:
                raise ArenaCaseError(f"Git blob size changed during reconstruction: {entry.path}")
            _write_file(staging, entry.path, blob, entry.mode)
        _apply_patch(case, staging)
        if overlay is not None:
            _extract_overlay(overlay, staging)
        _scan_leakage(case, staging)
        actual_hash = tree_digest(staging)
        if actual_hash != case.prepared_tree_sha256:
            raise ArenaCaseError(
                f"prepared tree hash mismatch: expected {case.prepared_tree_sha256}, got {actual_hash}"
            )
        provenance: dict[str, object] = {
            "schema": 1,
            "case_id": case.id,
            "source_id": case.source.id,
            "source_commit": case.source.commit,
            "source_subdir": case.source.subdir,
            "prepared_tree_sha256": actual_hash,
            "fidelity": case.fidelity,
            "caveats": list(case.caveats),
        }
        provenance_path = staging / PROVENANCE_FILE
        provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        provenance_path.chmod(0o644)
        if target.exists() or target.is_symlink():
            raise ArenaCaseError(f"destination appeared during preparation: {target}")
        try:
            os.rename(staging, target)
        except OSError as exc:
            if target.exists() or target.is_symlink():
                raise ArenaCaseError(f"destination appeared during publication: {target}") from exc
            raise ArenaCaseError(f"could not publish prepared workspace: {target}: {exc}") from exc
        return provenance
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
