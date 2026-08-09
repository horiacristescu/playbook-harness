"""Strict schema for portable historical cases."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


SCHEMA = 1
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ArenaCaseError(RuntimeError):
    """Expected case-data or reconstruction error."""


def _object(value: Any, fields: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ArenaCaseError(f"{label} must be an object")
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unknown {', '.join(unknown)}")
        raise ArenaCaseError(f"{label} fields invalid: {'; '.join(details)}")
    return value


def _text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ArenaCaseError(f"{label} must be non-empty text without NUL")
    return value


def _identifier(value: Any, *, label: str) -> str:
    text = _text(value, label=label)
    if IDENTIFIER_RE.fullmatch(text) is None:
        raise ArenaCaseError(f"{label} is not a safe identifier: {text!r}")
    return text


def safe_relative(value: Any, *, label: str) -> str:
    text = _text(value, label=label)
    if "\\" in text:
        raise ArenaCaseError(f"{label} must use POSIX separators")
    path = PurePosixPath(text)
    raw_parts = text.split("/")
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise ArenaCaseError(f"{label} must be a safe relative path: {text!r}")
    return path.as_posix()


def _string_list(value: Any, *, label: str, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ArenaCaseError(f"{label} must be a list")
    items = tuple(_text(item, label=f"{label} item") for item in value)
    if not allow_empty and not items:
        raise ArenaCaseError(f"{label} must not be empty")
    if len(items) != len(set(items)):
        raise ArenaCaseError(f"{label} must not contain duplicates")
    return items


@dataclass(frozen=True)
class SourceSpec:
    id: str
    commit: str
    subdir: str


@dataclass(frozen=True)
class OverlaySpec:
    sha256: str
    format: str


@dataclass(frozen=True)
class CaseDefinition:
    root: Path
    id: str
    title: str
    description: str
    fidelity: str
    caveats: tuple[str, ...]
    source: SourceSpec
    patch: str | None
    overlay: OverlaySpec | None
    forbidden_paths: tuple[str, ...]
    forbidden_content: tuple[str, ...]
    tools: tuple[str, ...]
    provenance: Mapping[str, str]
    input_hashes: Mapping[str, str]
    prepared_tree_sha256: str


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink():
        raise ArenaCaseError(f"{label} may not be a symlink: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArenaCaseError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ArenaCaseError(f"{label} must be a JSON object")
    return value


def load_case(case_dir: str | Path) -> CaseDefinition:
    root = Path(case_dir)
    if root.is_symlink() or not root.is_dir():
        raise ArenaCaseError(f"case directory must be a real directory: {root}")
    root = root.resolve()
    manifest = _object(
        _read_json(root / "case.json", label="case manifest"),
        {"schema", "id", "title", "description", "fidelity", "caveats", "source", "preparation", "leakage", "requirements", "provenance"},
        label="case manifest",
    )
    if type(manifest["schema"]) is not int or manifest["schema"] != SCHEMA:
        raise ArenaCaseError(f"case schema must be {SCHEMA}")
    case_id = _identifier(manifest["id"], label="case id")
    if root.name != case_id:
        raise ArenaCaseError(f"case id {case_id!r} does not match directory {root.name!r}")
    fidelity = manifest["fidelity"]
    if fidelity not in {"exact", "caveated"}:
        raise ArenaCaseError("fidelity must be 'exact' or 'caveated'")
    caveats = _string_list(manifest["caveats"], label="caveats")
    if (fidelity == "exact" and caveats) or (fidelity == "caveated" and not caveats):
        raise ArenaCaseError("exact cases need no caveats; caveated cases need at least one")

    source = _object(manifest["source"], {"id", "kind", "commit", "subdir"}, label="source")
    if source["kind"] != "local_git":
        raise ArenaCaseError("source kind must be 'local_git' in schema 1")
    commit = _text(source["commit"], label="source commit")
    if SHA1_RE.fullmatch(commit) is None:
        raise ArenaCaseError("source commit must be a full lowercase SHA-1 object id")
    source_spec = SourceSpec(
        id=_identifier(source["id"], label="source id"),
        commit=commit,
        subdir=safe_relative(source["subdir"], label="source subdir"),
    )

    preparation = _object(manifest["preparation"], {"patch", "overlay"}, label="preparation")
    patch = None if preparation["patch"] is None else safe_relative(preparation["patch"], label="patch")
    overlay_value = preparation["overlay"]
    overlay = None
    if overlay_value is not None:
        overlay_data = _object(overlay_value, {"sha256", "format"}, label="overlay")
        if not isinstance(overlay_data["sha256"], str) or SHA256_RE.fullmatch(overlay_data["sha256"]) is None:
            raise ArenaCaseError("overlay sha256 must be 64 lowercase hex characters")
        if overlay_data["format"] != "tar.gz":
            raise ArenaCaseError("overlay format must be 'tar.gz' in schema 1")
        overlay = OverlaySpec(overlay_data["sha256"], overlay_data["format"])

    leakage = _object(manifest["leakage"], {"forbidden_paths", "forbidden_content"}, label="leakage")
    requirements = _object(manifest["requirements"], {"tools"}, label="requirements")
    tools = tuple(_identifier(item, label="tool") for item in _string_list(requirements["tools"], label="tools"))
    provenance_fields = {"historical_task", "source_note", "transcript_status", "rubric_status"}
    provenance = _object(manifest["provenance"], provenance_fields, label="provenance")
    provenance_text = {key: _text(value, label=f"provenance {key}") for key, value in provenance.items()}

    checksums = _object(
        _read_json(root / "checksums.json", label="case checksums"),
        {"schema", "case_id", "inputs", "prepared_tree_sha256"},
        label="case checksums",
    )
    if type(checksums["schema"]) is not int or checksums["schema"] != SCHEMA:
        raise ArenaCaseError(f"checksum schema must be {SCHEMA}")
    if checksums["case_id"] != case_id:
        raise ArenaCaseError("checksum case_id does not match manifest")
    inputs = checksums["inputs"]
    if not isinstance(inputs, dict):
        raise ArenaCaseError("checksum inputs must be an object")
    input_hashes: dict[str, str] = {}
    for name, digest in inputs.items():
        safe_name = safe_relative(name, label="checksum input")
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise ArenaCaseError(f"checksum input hash is invalid: {safe_name}")
        input_hashes[safe_name] = digest
    tree_hash = checksums["prepared_tree_sha256"]
    if not isinstance(tree_hash, str) or SHA256_RE.fullmatch(tree_hash) is None:
        raise ArenaCaseError("prepared_tree_sha256 must be 64 lowercase hex characters")
    if patch is not None and patch not in input_hashes:
        raise ArenaCaseError("patch is missing from checksum inputs")
    if overlay is not None and f"overlay/{overlay.sha256}.tar.gz" not in input_hashes:
        raise ArenaCaseError("overlay is missing from checksum inputs")
    if overlay is not None and input_hashes[f"overlay/{overlay.sha256}.tar.gz"] != overlay.sha256:
        raise ArenaCaseError("overlay checksum input must equal its content-addressed sha256")

    return CaseDefinition(
        root=root,
        id=case_id,
        title=_text(manifest["title"], label="title"),
        description=_text(manifest["description"], label="description"),
        fidelity=fidelity,
        caveats=caveats,
        source=source_spec,
        patch=patch,
        overlay=overlay,
        forbidden_paths=tuple(
            safe_relative(item, label="forbidden path")
            for item in _string_list(leakage["forbidden_paths"], label="forbidden_paths")
        ),
        forbidden_content=_string_list(leakage["forbidden_content"], label="forbidden_content"),
        tools=tools,
        provenance=provenance_text,
        input_hashes=input_hashes,
        prepared_tree_sha256=tree_hash,
    )


def discover_cases(cases_root: str | Path) -> list[CaseDefinition]:
    root = Path(cases_root)
    if root.is_symlink() or not root.is_dir():
        raise ArenaCaseError(f"cases root must be a real directory: {root}")
    cases = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.is_symlink():
            cases.append(load_case(child))
    return cases
