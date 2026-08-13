"""Identity and compatibility contract for the installed Playbook runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path


RUNTIME_COMPAT_SCHEMA = 1
PROJECT_RUNTIME_FILE = ".agent/playbook-runtime.json"


def runtime_root() -> Path:
    return Path(__file__).resolve().parents[2]


def runtime_commit() -> str:
    root = runtime_root()
    git_dir = root / ".git"
    if not git_dir.exists():
        manifest = root / ".playbook-artifact.json"
        try:
            commit = json.loads(manifest.read_text(encoding="utf-8"))["commit"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "installed Playbook runtime identity is unavailable"
            ) from exc
        if not isinstance(commit, str) or len(commit) != 40 or any(
            ch not in "0123456789abcdef" for ch in commit
        ):
            raise RuntimeError("installed Playbook artifact commit is invalid")
        return commit
    if git_dir.is_file():
        pointer = git_dir.read_text(encoding="utf-8").strip()
        if pointer.startswith("gitdir: "):
            git_dir = (git_dir.parent / pointer.removeprefix("gitdir: ")).resolve()
    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    if head.startswith("ref: "):
        ref = head.removeprefix("ref: ")
        ref_file = git_dir / ref
        if ref_file.is_file():
            commit = ref_file.read_text(encoding="utf-8").strip()
        else:
            commit = ""
            packed = git_dir / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.endswith(f" {ref}"):
                        commit = line.split(" ", 1)[0]
                        break
    else:
        commit = head
    if len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
        raise RuntimeError("installed Playbook runtime Git commit is unavailable")
    return commit


def runtime_identity() -> dict[str, object]:
    """Return the identity of the code serving this exact CLI invocation."""
    from .core import VERSION

    root = runtime_root()
    executable = os.environ.get("PLAYBOOK_TASKS_EXECUTABLE")
    if executable:
        executable_path = str(Path(executable).expanduser().resolve())
    else:
        executable_path = f"python-module:{Path(__file__).resolve()}"
    return {
        "runtime_schema": RUNTIME_COMPAT_SCHEMA,
        "version": VERSION,
        "commit": runtime_commit(),
        "kind": "public-artifact" if (root / ".playbook-artifact.json").is_file() else "development-checkout",
        "root": str(root),
        "executable": executable_path,
    }


def project_runtime_expectation(project_root: Path) -> dict[str, object] | None:
    """Read the runtime generation recorded by the most recent successful init."""
    marker = project_root / PROJECT_RUNTIME_FILE
    if not marker.is_file():
        return None
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid project runtime marker: {marker}") from exc
    required = ("runtime_schema", "version", "commit")
    if not isinstance(value, dict) or any(field not in value for field in required):
        raise RuntimeError(f"incomplete project runtime marker: {marker}")
    commit = value["commit"]
    if (
        not isinstance(value["runtime_schema"], int)
        or not isinstance(value["version"], str)
        or not value["version"]
        or not isinstance(commit, str)
        or len(commit) != 40
        or any(ch not in "0123456789abcdef" for ch in commit)
    ):
        raise RuntimeError(f"invalid project runtime identity: {marker}")
    return {field: value[field] for field in required}


def runtime_generation_status(project_root: Path) -> tuple[str, bool]:
    """Render acting runtime identity and diagnose project-generation skew."""
    acting = runtime_identity()
    executable = acting["executable"]
    line = (
        f"Playbook runtime: version={acting['version']} schema={acting['runtime_schema']} "
        f"commit={str(acting['commit'])[:12]} kind={acting['kind']} "
        f"executable={executable} root={acting['root']}"
    )
    expected = project_runtime_expectation(project_root)
    if expected is None:
        return line + "\nProject generation: unrecorded (run pb-tasks init to record it)", True
    matches = all(expected[field] == acting[field] for field in expected)
    project_line = (
        f"Project generation: version={expected['version']} schema={expected['runtime_schema']} "
        f"commit={str(expected['commit'])[:12]} status={'current' if matches else 'SKEW'}"
    )
    if not matches:
        project_line += (
            "; this project was initialized by a different Playbook runtime. "
            "Use the executable/root shown above, then upgrade or rerun pb-tasks init before ownership changes."
        )
    return line + "\n" + project_line, matches
