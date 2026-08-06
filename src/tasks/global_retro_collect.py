"""Collect Playbook control-plane artifacts for a global retrospective.

Discovery rule: a project is kept only when it contains `.agent/tasks/` with
at least one `NNN-<slug>/task.md` child. This mirrors the normal Playbook
project-root rule and avoids treating arbitrary `.agent/` folders as Playbook
projects.

Cutoff rule: each task is compared to the cutoff using the git first-add commit
time for its `task.md` when available. If git is unavailable or the file is
untracked, the fallback is the oldest mtime among files in the task directory.
The manifest records the chosen timestamp and source per task.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import socket
import stat
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


TOOL_VERSION = "global-retro-collect-v1"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
FIXED_ARCHIVE_DT = (1980, 1, 1, 0, 0, 0)
FIXED_ARCHIVE_TS = 315532800

TASK_DIR_RE = re.compile(r"^(?P<number>\d{3})-(?P<slug>.+)$")

TOP_LEVEL_ALLOW = {
    "MIND_MAP.md",
    "MIND_MAP_OVERFLOW.md",
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
}
AGENT_FILE_ALLOW = {"chat_log.md", "bash_history", "bash_log.md"}
MONITOR_ALLOW = {"session.md", "rules.md", "MONITOR_MIND_MAP.md", "trace.md", "nudge.md"}
HOOK_CONFIGS = (Path(".claude/settings.json"), Path(".codex/hooks.json"))

HARD_EXCLUDED_NAMES = {".DS_Store", ".offset", ".pid", "sandbox.sb"}
HARD_EXCLUDED_SUFFIXES = {".py", ".pyc"}
HARD_EXCLUDED_PARTS = {"__pycache__"}


@dataclass(frozen=True)
class CollectedFile:
    member: str
    data: bytes


def parse_cutoff(value: str) -> dt.datetime:
    raw = value.strip()
    if not raw:
        raise ValueError("cutoff date is empty")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return dt.datetime.fromisoformat(raw).replace(tzinfo=dt.timezone.utc)
    parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def collect_global_retro(
    roots: Iterable[Path | str],
    since: str | dt.datetime,
    out_dir: Path | str,
    *,
    machine: str | None = None,
    archive_format: str = "zip",
    now: dt.datetime | None = None,
    max_file_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[Path, dict]:
    cutoff = parse_cutoff(since) if isinstance(since, str) else _ensure_utc(since)
    now = _ensure_utc(now or dt.datetime.now(dt.timezone.utc))
    machine = machine or socket.gethostname()
    archive_format = archive_format.lower()
    if archive_format not in {"zip", "tgz"}:
        raise ValueError("--format must be 'zip' or 'tgz'")

    root_paths = [Path(r).expanduser().resolve() for r in roots]
    if not root_paths:
        raise ValueError("at least one root directory is required")
    for root in root_paths:
        if not root.exists():
            raise ValueError(f"root directory not found: {root}")

    out_path = Path(out_dir).expanduser().resolve()
    out_path.mkdir(parents=True, exist_ok=True)
    safe_machine = _safe_component(machine)
    cutoff_label = cutoff.date().isoformat()
    now_label = now.strftime("%Y%m%dT%H%M%SZ")
    suffix = "zip" if archive_format == "zip" else "tgz"
    archive_path = out_path / f"playbook-retro-{safe_machine}-{cutoff_label}-{now_label}.{suffix}"

    projects = []
    files: list[CollectedFile] = []

    for project, discovery_reason in _discover_candidates(root_paths):
        path_slug = _path_slug(project)
        entry = {
            "abs_path": str(project),
            "path_slug": path_slug,
            "project_name": project.name,
            "discovery_reason": discovery_reason,
            "kept": False,
            "skip_reason": "",
            "task_count": 0,
            "included_tasks": [],
            "included_files": [],
            "skipped_files": [],
        }
        task_dirs = _valid_task_dirs(project)
        entry["task_count"] = len(task_dirs)
        if not task_dirs:
            entry["skip_reason"] = "no valid task.md children"
            projects.append(entry)
            continue

        included_task_dirs = []
        for task_dir in task_dirs:
            cutoff_ts, cutoff_source = _task_cutoff_timestamp(project, task_dir)
            if cutoff_ts >= cutoff:
                match = TASK_DIR_RE.match(task_dir.name)
                included_task_dirs.append(task_dir)
                entry["included_tasks"].append({
                    "number": int(match.group("number")) if match else None,
                    "slug": match.group("slug") if match else task_dir.name,
                    "dir": task_dir.name,
                    "cutoff_ts": cutoff_ts.isoformat().replace("+00:00", "Z"),
                    "cutoff_source": cutoff_source,
                })

        if not included_task_dirs:
            entry["skip_reason"] = "no tasks at or after cutoff"
            projects.append(entry)
            continue

        entry["kept"] = True
        selected = _select_project_files(project, included_task_dirs, max_file_bytes)
        for relpath, reason in selected["skipped"]:
            entry["skipped_files"].append({"path": _posix(relpath), "reason": reason})
        for relpath in selected["included"]:
            data = (project / relpath).read_bytes()
            member = _member_name(machine, path_slug, relpath)
            _validate_member(member)
            files.append(CollectedFile(member=member, data=data))
            entry["included_files"].append(_posix(relpath))
        projects.append(entry)

    manifest = {
        "tool_version": TOOL_VERSION,
        "machine": machine,
        "machine_slug": safe_machine,
        "cutoff": cutoff.isoformat().replace("+00:00", "Z"),
        "format": archive_format,
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "roots": [str(r) for r in root_paths],
        "projects": projects,
    }

    payloads = _payloads_with_manifests(files, manifest)
    if archive_format == "zip":
        _write_zip(archive_path, payloads)
    else:
        _write_tgz(archive_path, payloads)
    return archive_path, manifest


def archive_member_names(path: Path) -> list[str]:
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            return sorted(zf.namelist())
    with tarfile.open(path, "r:gz") as tf:
        return sorted(tf.getnames())


def _ensure_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _discover_candidates(roots: list[Path]) -> list[tuple[Path, str]]:
    found: dict[Path, str] = {}
    prune = {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
        ".venv",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "node_modules",
        "target",
        "venv",
        "__pycache__",
    }
    for root in roots:
        for dirpath, dirnames, _filenames in os.walk(root, topdown=True):
            dirnames[:] = [d for d in dirnames if d not in prune]
            if ".agent" not in dirnames:
                continue
            project = Path(dirpath).resolve()
            agent = project / ".agent"
            if (agent / "tasks").exists():
                found.setdefault(project, "has .agent/tasks")
                dirnames[:] = []
            else:
                found.setdefault(project, "skipped: .agent without tasks")
                dirnames.remove(".agent")
    return sorted(found.items(), key=lambda item: str(item[0]))


def _valid_task_dirs(project: Path) -> list[Path]:
    tasks_dir = project / ".agent" / "tasks"
    if not tasks_dir.exists():
        return []
    result = []
    for child in tasks_dir.iterdir():
        if child.is_dir() and TASK_DIR_RE.match(child.name) and (child / "task.md").is_file():
            result.append(child)
    return sorted(result, key=lambda p: p.name)


def _task_cutoff_timestamp(project: Path, task_dir: Path) -> tuple[dt.datetime, str]:
    git_ts = _git_first_add_timestamp(project, task_dir / "task.md")
    if git_ts is not None:
        return git_ts, "git_first_add"
    oldest = None
    for path in task_dir.rglob("*"):
        if path.is_file() and not path.is_symlink():
            mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
            oldest = mtime if oldest is None or mtime < oldest else oldest
    if oldest is None:
        oldest = dt.datetime.fromtimestamp(task_dir.stat().st_mtime, tz=dt.timezone.utc)
    return oldest, "oldest_file_mtime"


def _git_first_add_timestamp(project: Path, task_file: Path) -> dt.datetime | None:
    try:
        rel = task_file.relative_to(project)
    except ValueError:
        return None
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(project),
                "log",
                "--diff-filter=A",
                "--follow",
                "--format=%cI",
                "--",
                _posix(rel),
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return parse_cutoff(lines[-1])
    except ValueError:
        return None


def _select_project_files(project: Path, task_dirs: list[Path], max_file_bytes: int) -> dict[str, list]:
    included: set[Path] = set()
    skipped: list[tuple[Path, str]] = []

    def consider(relpath: Path) -> None:
        file_path = project / relpath
        ok, reason = _is_allowed_file(project, relpath, max_file_bytes)
        if ok:
            included.add(relpath)
        elif file_path.exists():
            skipped.append((relpath, reason))

    for name in sorted(TOP_LEVEL_ALLOW):
        consider(Path(name))
    for name in sorted(AGENT_FILE_ALLOW):
        consider(Path(".agent") / name)
    for relpath in HOOK_CONFIGS:
        consider(relpath)

    playbooks = project / ".agent" / "playbooks"
    if playbooks.exists():
        for path in sorted(playbooks.rglob("*.md")):
            consider(path.relative_to(project))

    monitor = project / ".agent" / "monitor"
    if monitor.exists():
        for name in sorted(MONITOR_ALLOW):
            consider(Path(".agent") / "monitor" / name)

    for task_dir in task_dirs:
        for path in sorted(task_dir.rglob("*")):
            if path.is_file():
                consider(path.relative_to(project))

    return {
        "included": sorted(included, key=_posix),
        "skipped": sorted(skipped, key=lambda item: _posix(item[0])),
    }


def _is_allowed_file(project: Path, relpath: Path, max_file_bytes: int) -> tuple[bool, str]:
    file_path = project / relpath
    if not file_path.exists():
        return False, "missing"
    if file_path.is_symlink():
        return False, "symlink"
    if not file_path.is_file():
        return False, "not a regular file"
    parts = set(relpath.parts)
    if parts & HARD_EXCLUDED_PARTS:
        return False, "hard-excluded path component"
    if relpath.name in HARD_EXCLUDED_NAMES:
        return False, "hard-excluded file name"
    if relpath.suffix in HARD_EXCLUDED_SUFFIXES:
        return False, "hard-excluded suffix"
    posix = _posix(relpath)
    if posix.startswith(".agent/sessions/") or posix.startswith(".agent/monitor/pids/"):
        return False, "hard-excluded state directory"
    try:
        st = file_path.stat()
    except OSError:
        return False, "stat failed"
    if st.st_size > max_file_bytes:
        return False, "size cap"
    if st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        return False, "executable"
    if _looks_binary(file_path):
        return False, "binary"
    return True, ""


def _looks_binary(path: Path, sample_size: int = 4096) -> bool:
    try:
        with path.open("rb") as fh:
            sample = fh.read(sample_size)
    except OSError:
        return True
    if b"\0" in sample:
        return True
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _payloads_with_manifests(files: list[CollectedFile], manifest: dict) -> list[tuple[str, bytes]]:
    payloads = [(f.member, f.data) for f in files]
    manifest_json = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    manifest_tsv = _manifest_tsv(manifest).encode("utf-8")
    payloads.append(("manifest.json", manifest_json))
    payloads.append(("manifest.tsv", manifest_tsv))
    return sorted(payloads, key=lambda item: item[0])


def _manifest_tsv(manifest: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow([
        "machine",
        "project_name",
        "path_slug",
        "abs_path",
        "kept",
        "skip_reason",
        "task_count",
        "included_task_count",
        "included_file_count",
    ])
    for project in manifest["projects"]:
        writer.writerow([
            manifest["machine"],
            project["project_name"],
            project["path_slug"],
            project["abs_path"],
            project["kept"],
            project["skip_reason"],
            project["task_count"],
            len(project["included_tasks"]),
            len(project["included_files"]),
        ])
    return buffer.getvalue()


def _write_zip(path: Path, payloads: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in payloads:
            info = zipfile.ZipInfo(name, FIXED_ARCHIVE_DT)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            zf.writestr(info, data)


def _write_tgz(path: Path, payloads: list[tuple[str, bytes]]) -> None:
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tf:
                for name, data in payloads:
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    info.mtime = FIXED_ARCHIVE_TS
                    info.mode = 0o644
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    tf.addfile(info, io.BytesIO(data))


def _member_name(machine: str, path_slug: str, relpath: Path) -> str:
    return f"{_safe_component(machine)}/{path_slug}/{_posix(relpath)}"


def _validate_member(member: str) -> None:
    pure = PurePosixPath(member)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"unsafe archive member path: {member}")


def _path_slug(path: Path, max_prefix: int = 80) -> str:
    raw = str(path.resolve())
    normalized = raw.replace("\\", "/")
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]
    cleaned = normalized.strip("/").replace(":", "")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "__", cleaned)
    cleaned = cleaned[:max_prefix].strip("._-") or "root"
    return f"{cleaned}-{digest}"


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return cleaned.strip(".-") or "machine"


def _posix(path: Path) -> str:
    return path.as_posix()
