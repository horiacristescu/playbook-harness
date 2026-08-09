"""Materialize one frozen Playbook runtime variant from local Git objects."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath

from .case import ArenaCaseError, safe_relative
from .git_source import GIT_ENV, read_blob, reserve_staging
from .prepare import MAX_GIT_FILE_BYTES, MAX_GIT_TOTAL_BYTES, _preflight_patch, tree_digest
from .schema import VariantDefinition


def _git(repo: Path, arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment.update(GIT_ENV)
    try:
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *arguments], env=environment, capture_output=True, check=False)
    except OSError as exc:
        raise ArenaCaseError(f"could not run git for variant: {exc}") from exc
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ArenaCaseError(detail or "variant Git operation failed")
    return result


def _resolve_repo(value: str | Path, commit: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise ArenaCaseError(f"runtime repository may not be a symlink: {candidate}")
    try:
        repo = candidate.resolve(strict=True)
    except OSError as exc:
        raise ArenaCaseError(f"runtime repository is unavailable: {candidate}: {exc}") from exc
    if not repo.is_dir() or _git(repo, ["rev-parse", "--is-inside-work-tree"], check=False).stdout.strip() != b"true":
        raise ArenaCaseError(f"runtime source is not a Git work tree: {repo}")
    if _git(repo, ["cat-file", "-e", f"{commit}^{{commit}}"], check=False).returncode != 0:
        raise ArenaCaseError(f"variant base commit is unavailable: {commit}")
    return repo


def _tree(repo: Path, commit: str) -> list[tuple[str, str, int, int]]:
    result = _git(repo, ["ls-tree", "-lrz", "-r", "--full-tree", commit])
    entries = []
    folded = set()
    total = 0
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        fields = header.split()
        if not separator or len(fields) != 4:
            raise ArenaCaseError("git returned malformed variant tree data")
        mode_raw, kind, object_raw, size_raw = fields
        try:
            relative = safe_relative(raw_path.decode("utf-8", errors="strict"), label="variant tree path")
            size = int(size_raw)
        except (UnicodeError, ValueError) as exc:
            raise ArenaCaseError("variant tree has unsupported filename or size") from exc
        if kind != b"blob" or mode_raw not in {b"100644", b"100755"}:
            raise ArenaCaseError(f"unsupported variant tree entry: {relative}")
        if size > MAX_GIT_FILE_BYTES:
            raise ArenaCaseError(f"variant runtime file is too large: {relative}")
        total += size
        if total > MAX_GIT_TOTAL_BYTES:
            raise ArenaCaseError("variant runtime tree is too large")
        if relative.casefold() in folded:
            raise ArenaCaseError(f"case-colliding variant paths are forbidden: {relative}")
        folded.add(relative.casefold())
        entries.append((relative, object_raw.decode("ascii"), int(mode_raw, 8), size))
    return sorted(entries)


def _patch_paths(data: bytes) -> set[str]:
    _preflight_patch(data)
    paths = set()
    for line in data.decode("utf-8", errors="strict").splitlines():
        if line.startswith(("old mode ", "new mode ", "new file mode ", "deleted file mode ")) or line in {"GIT binary patch"} or line.startswith("Binary files "):
            raise ArenaCaseError("variant patches may not change modes, add/delete files, or contain binary deltas")
        if line.startswith("diff --git "):
            fields = line.split()
            left = safe_relative(fields[2][2:], label="variant patch path")
            right = safe_relative(fields[3][2:], label="variant patch path")
            if left != right:
                raise ArenaCaseError("variant patch renames are unsupported in schema 1")
            paths.add(left)
    if not paths:
        raise ArenaCaseError("variant patch has no declared diff paths")
    return paths


def prepare_variant_runtime(variant: VariantDefinition, runtime_repo: str | Path, destination: str | Path) -> dict[str, str]:
    repo = _resolve_repo(runtime_repo, variant.base_commit)
    target = Path(destination).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    staging = reserve_staging(target)
    try:
        for relative, object_id, mode, expected_size in _tree(repo, variant.base_commit):
            data = read_blob(repo, object_id)
            if len(data) != expected_size:
                raise ArenaCaseError(f"variant blob size mismatch: {relative}")
            output = staging.joinpath(*PurePosixPath(relative).parts)
            output.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
            output.write_bytes(data)
            output.chmod(0o755 if mode & 0o111 else 0o644)
        if variant.patch is not None:
            patch = variant.path.parent.joinpath(*PurePosixPath(variant.patch).parts)
            data = patch.read_bytes()
            actual = _patch_paths(data)
            if actual != set(variant.touched_paths):
                raise ArenaCaseError("variant patch paths do not match touched_paths")
            environment = os.environ.copy()
            environment.update(GIT_ENV)
            result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "apply", "--whitespace=nowarn", "--", str(patch)], cwd=staging, env=environment, capture_output=True, check=False)
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", errors="replace").strip()
                raise ArenaCaseError(f"variant patch failed: {detail}")
        digest = tree_digest(staging)
        os.replace(staging, target)
        return {"variant_id": variant.id, "base_commit": variant.base_commit, "runtime_tree_sha256": digest}
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
