"""Identity and compatibility contract for the installed Playbook runtime."""

from __future__ import annotations

from pathlib import Path


RUNTIME_COMPAT_SCHEMA = 1


def runtime_root() -> Path:
    return Path(__file__).resolve().parents[2]


def runtime_commit() -> str:
    git_dir = runtime_root() / ".git"
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
