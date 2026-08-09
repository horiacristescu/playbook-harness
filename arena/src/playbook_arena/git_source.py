"""Read exact historical blobs from explicitly bound local Git object stores."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from .case import ArenaCaseError, SourceSpec, safe_relative


GIT_ENV = {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_CONFIG_NOSYSTEM": "1"}


@dataclass(frozen=True)
class GitEntry:
    path: str
    object_id: str
    mode: int
    size: int


def parse_source_bindings(values: Sequence[str]) -> dict[str, Path]:
    bindings: dict[str, Path] = {}
    for value in values:
        source_id, separator, raw_path = value.partition("=")
        if not separator or not source_id or not raw_path:
            raise ArenaCaseError(f"invalid source binding {value!r}; expected ID=PATH")
        if source_id in bindings:
            raise ArenaCaseError(f"duplicate source binding: {source_id}")
        bindings[source_id] = Path(raw_path).expanduser()
    return bindings


def _git(repo: Path, arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment.update(GIT_ENV)
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *arguments],
            env=environment,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise ArenaCaseError(f"could not run git: {exc}") from exc
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ArenaCaseError(detail or f"git command failed: {arguments[0]}")
    return result


def resolve_source(spec: SourceSpec, bindings: Mapping[str, Path]) -> Path:
    if spec.id not in bindings:
        raise ArenaCaseError(f"source {spec.id!r} is unbound; pass --source {spec.id}=PATH")
    candidate = bindings[spec.id]
    if candidate.is_symlink():
        raise ArenaCaseError(f"source repository may not be a symlink: {candidate}")
    try:
        repository = candidate.resolve(strict=True)
    except OSError as exc:
        raise ArenaCaseError(f"source repository is unavailable: {candidate}: {exc}") from exc
    if not repository.is_dir():
        raise ArenaCaseError(f"source repository is not a directory: {repository}")
    inside = _git(repository, ["rev-parse", "--is-inside-work-tree"], check=False)
    if inside.returncode != 0 or inside.stdout.strip() != b"true":
        raise ArenaCaseError(f"source is not a Git work tree: {repository}")
    if _git(repository, ["cat-file", "-e", f"{spec.commit}^{{commit}}"], check=False).returncode != 0:
        raise ArenaCaseError(f"source commit is unavailable: {spec.commit}")
    tree_type = _git(repository, ["cat-file", "-t", f"{spec.commit}:{spec.subdir}"], check=False)
    if tree_type.returncode != 0 or tree_type.stdout.strip() != b"tree":
        raise ArenaCaseError(f"source subdirectory is unavailable at commit: {spec.subdir}")
    return repository


def list_source_tree(repo: Path, spec: SourceSpec) -> list[GitEntry]:
    result = _git(repo, ["ls-tree", "-lrz", "-r", "--full-tree", spec.commit, "--", spec.subdir])
    prefix = f"{spec.subdir}/"
    entries: list[GitEntry] = []
    seen_casefolded: set[str] = set()
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        fields = header.split()
        if not separator or len(fields) != 4:
            raise ArenaCaseError("git returned malformed tree data")
        mode_raw, kind, object_raw, size_raw = fields
        try:
            path = raw_path.decode("utf-8", errors="strict")
            size = int(size_raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ArenaCaseError("Git tree contains an unsupported filename or size") from exc
        if not path.startswith(prefix):
            raise ArenaCaseError(f"git tree escaped source subdirectory: {path}")
        relative = safe_relative(path[len(prefix) :], label="Git tree path")
        if kind != b"blob" or mode_raw not in {b"100644", b"100755"}:
            raise ArenaCaseError(f"unsupported Git tree entry: {path} mode={mode_raw.decode()}")
        folded = relative.casefold()
        if folded in seen_casefolded:
            raise ArenaCaseError(f"case-colliding Git paths are forbidden: {relative}")
        seen_casefolded.add(folded)
        entries.append(GitEntry(relative, object_raw.decode("ascii"), int(mode_raw, 8), size))
    return sorted(entries, key=lambda entry: entry.path)


def read_blob(repo: Path, object_id: str) -> bytes:
    return _git(repo, ["cat-file", "blob", object_id]).stdout


def reserve_staging(destination: str | Path) -> Path:
    target = Path(destination).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    if target.exists() or target.is_symlink():
        raise ArenaCaseError(f"destination already exists: {target}")
    parent = target.parent.resolve(strict=True)
    if not parent.is_dir():
        raise ArenaCaseError(f"destination parent is not a directory: {parent}")
    import tempfile

    return Path(tempfile.mkdtemp(prefix=f".{target.name}.arena-", dir=parent))
