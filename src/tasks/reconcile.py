"""Project-local reconciliation primitives.

Task B keeps init-target selection separate from ordinary project discovery:
creation always targets the explicit path or cwd exactly, while established
commands may walk upward to the nearest initialized Harness project.
"""

from __future__ import annotations

import os
import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePath
from typing import Any, Callable, TypeAlias

from .core import resolve_agent_dir


class ReconcileError(ValueError):
    """A reconciliation request is unsafe or cannot be interpreted."""


MANAGED_BY = "playbook-harness"
MANAGED_SCHEMA = 2


class OperationState(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class DirectoryIntent:
    relative: str
    mode: int = 0o755
    hook: bool = False


@dataclass(frozen=True)
class ManagedFileIntent:
    relative: str
    body: str
    source: str
    mode: int = 0o644
    marker_style: str = "hash"
    hook: bool = False
    adopt_hashes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CreateOnlyFileIntent:
    relative: str
    content: str
    mode: int = 0o644


@dataclass(frozen=True)
class ExactFileIntent:
    """One authenticated, unmarked file transition used by migrations."""

    relative: str
    content: str
    expected_digest: str
    mode: int = 0o644
    hook: bool = False


@dataclass(frozen=True)
class ExactDeleteIntent:
    """Delete one authenticated regular file during a migration."""

    relative: str
    expected_digest: str
    expected_mode: int = 0o644
    hook: bool = False


@dataclass(frozen=True)
class SharedBlockIntent:
    relative: str
    body: str
    source: str
    mode: int = 0o644
    hook: bool = False


@dataclass(frozen=True)
class JsonEntry:
    path: tuple[str, ...]
    value: Any
    adopt_hashes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SharedJsonIntent:
    relative: str
    entries: tuple[JsonEntry, ...]
    mode: int = 0o644
    hook: bool = False


@dataclass(frozen=True)
class KeyedListEntry:
    path: tuple[str, ...]
    key_fields: tuple[str, ...]
    value: dict[str, Any]
    adopt_hashes: tuple[str, ...] = ()
    nested_list_field: str | None = None
    nested_key_fields: tuple[str, ...] = ()
    adopt_keys: tuple[tuple[Any, ...], ...] = ()


@dataclass(frozen=True)
class SharedKeyedListIntent:
    relative: str
    entries: tuple[KeyedListEntry, ...]
    mode: int = 0o644
    hook: bool = False


Intent: TypeAlias = (
    DirectoryIntent
    | ManagedFileIntent
    | CreateOnlyFileIntent
    | ExactFileIntent
    | ExactDeleteIntent
    | SharedBlockIntent
    | SharedJsonIntent
    | SharedKeyedListIntent
)


@dataclass(frozen=True)
class Contribution:
    provider: str
    intents: tuple[Intent, ...]


@dataclass(frozen=True)
class PlannedOperation:
    relative: str
    state: OperationState
    kind: str
    owners: tuple[str, ...]
    content: str | None = None
    mode: int = 0o644
    expected_digest: str | None = None


@dataclass(frozen=True)
class PlanConflict:
    relative: str
    reason: str
    owners: tuple[str, ...]


@dataclass(frozen=True)
class ReconciliationPlan:
    root: RootIdentity
    operations: tuple[PlannedOperation, ...]
    conflicts: tuple[PlanConflict, ...]

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)


@dataclass(frozen=True)
class AppliedGroup:
    name: str
    changed: tuple[str, ...]


@dataclass(frozen=True)
class ApplyResult:
    groups: tuple[AppliedGroup, ...]


class ApplyFailure(ReconcileError):
    def __init__(self, group: str, path: str, reason: str) -> None:
        self.group = group
        self.path = path
        self.reason = reason
        super().__init__(f"reconciliation group {group!r} failed at {path}: {reason}")


FaultHook: TypeAlias = Callable[[str, Path], None]


@dataclass(frozen=True)
class RootIdentity:
    """Stable identity used to notice a root replacement before mutation."""

    path: Path
    device: int
    inode: int

    @classmethod
    def capture(cls, root: Path) -> "RootIdentity":
        canonical = root.resolve(strict=True)
        if not canonical.is_dir():
            raise ReconcileError(f"project root is not a directory: {canonical}")
        stat = canonical.stat()
        return cls(canonical, stat.st_dev, stat.st_ino)

    def matches(self) -> bool:
        try:
            stat = self.path.stat()
        except OSError:
            return False
        return self.path.is_dir() and (stat.st_dev, stat.st_ino) == (
            self.device,
            self.inode,
        )


def resolve_init_target(explicit_path: Path | None, cwd: Path) -> Path:
    """Return the exact canonical init target; never adopt a parent project."""
    requested = explicit_path if explicit_path is not None else cwd
    requested = requested.expanduser()
    try:
        canonical = requested.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ReconcileError(f"directory not found: {requested}") from exc
    if not canonical.is_dir():
        raise ReconcileError(f"project target is not a directory: {canonical}")
    _validated_agent_dir(canonical)
    return canonical


def discover_initialized_root(cwd: Path) -> Path:
    """Walk upward from cwd and return the nearest initialized project root."""
    try:
        current = cwd.expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise ReconcileError(f"directory not found: {cwd}") from exc
    if not current.is_dir():
        raise ReconcileError(f"project discovery start is not a directory: {current}")

    while True:
        if is_initialized_root(current):
            return current
        if current.parent == current:
            break
        current = current.parent
    raise ReconcileError(f"no initialized Playbook project found from: {cwd}")


def is_initialized_root(root: Path) -> bool:
    """Return whether root has the canonical legacy or multi-user task marker."""
    agent_dir = _validated_agent_dir(root)
    tasks = agent_dir / "tasks"
    if tasks.is_symlink():
        raise ReconcileError(f"project task marker may not be a symlink: {tasks}")
    return tasks.is_dir()


def validate_relative_target(root: Path, relative: PurePath | str) -> Path:
    """Validate a planned root-relative path without following symlink components."""
    canonical = root.resolve(strict=True)
    path = PurePath(relative)
    if path.is_absolute() or not path.parts or any(
        part in ("", ".", "..") for part in path.parts
    ):
        raise ReconcileError(f"unsafe project-relative path: {relative!s}")

    target = canonical
    for part in path.parts:
        target = target / part
        if target.is_symlink():
            raise ReconcileError(f"project target component may not be a symlink: {target}")

    try:
        common = Path(os.path.commonpath((canonical, target.absolute())))
    except ValueError as exc:
        raise ReconcileError(f"project target escapes root: {relative!s}") from exc
    if common != canonical:
        raise ReconcileError(f"project target escapes root: {relative!s}")
    return target


def plan_reconciliation(
    root: Path,
    contributions: tuple[Contribution, ...],
    *,
    include_hooks: bool = True,
) -> ReconciliationPlan:
    """Compute a deterministic, read-only reconciliation plan."""
    identity = RootIdentity.capture(root)
    candidates: dict[str, list[tuple[str, Intent]]] = {}
    for contribution in sorted(contributions, key=lambda item: item.provider):
        if not contribution.provider:
            raise ReconcileError("contribution provider may not be empty")
        for intent in contribution.intents:
            if not include_hooks and getattr(intent, "hook", False):
                continue
            relative = intent.relative
            validate_relative_target(identity.path, relative)
            candidates.setdefault(relative, []).append((contribution.provider, intent))

    operations: list[PlannedOperation] = []
    conflicts: list[PlanConflict] = []
    for relative in sorted(candidates):
        items = candidates[relative]
        owners = tuple(sorted({provider for provider, _ in items}))
        intents = [intent for _, intent in items]
        first = intents[0]
        if any(intent != first for intent in intents[1:]):
            conflicts.append(
                PlanConflict(relative, "incompatible duplicate contributions", owners)
            )
            continue
        operation, conflict = _plan_intent(identity.path, first, owners)
        if conflict is not None:
            conflicts.append(conflict)
        elif operation is not None:
            operations.append(operation)

    return ReconciliationPlan(
        identity,
        tuple(operations),
        tuple(sorted(conflicts, key=lambda item: (item.relative, item.reason))),
    )


def shared_scaffold_contribution(
    project_root: Path,
    *,
    title: str | None = None,
) -> Contribution:
    """Return Task-B-owned, provider-neutral project scaffold intents."""
    root = project_root.resolve(strict=True)
    agent_dir = _validated_agent_dir(root)
    agent_relative = agent_dir.relative_to(root).as_posix()
    display = title or root.name.replace("-", " ").replace("_", " ").title()
    from .core import VERSION
    from .runtime import RUNTIME_COMPAT_SCHEMA, runtime_commit
    runtime_marker = json.dumps(
        {
            "runtime_schema": RUNTIME_COMPAT_SCHEMA,
            "version": VERSION,
            "commit": runtime_commit(),
        },
        indent=2,
        sort_keys=True,
    ) + "\n"
    ignore_body = """.agent/current_user
.agent/bash_history
.agent/*/bash_history
.agent/chat_log_counter
.agent/chat_log_counter.lock
.agent/sessions/
.agent/*/sessions/
.agent/narrative/narrative.html
.agent/narrative/entries.json
.agent/*/narrative/narrative.html
.agent/*/narrative/entries.json
"""
    return Contribution(
        "shared",
        (
            DirectoryIntent(f"{agent_relative}/tasks"),
            DirectoryIntent(".agent/templates"),
            ManagedFileIntent(
                ".agent/playbook-runtime.json",
                runtime_marker,
                "tasks/reconcile:project-runtime",
                marker_style="json",
            ),
            CreateOnlyFileIntent(
                "MIND_MAP.md",
                f"# {display}\n\n## Architecture\n\n(describe your project architecture here)\n",
            ),
            SharedBlockIntent(
                ".gitignore",
                ignore_body,
                "tasks/reconcile:project-gitignore",
            ),
        ),
    )


def render_managed_file(intent: ManagedFileIntent) -> str:
    """Render a whole-file ownership marker whose hash covers the body."""
    metadata = {
        "managed_by": MANAGED_BY,
        "schema": MANAGED_SCHEMA,
        "source": intent.source,
        "source_hash": _managed_body_digest(intent.body, intent.marker_style),
    }
    if intent.marker_style == "json":
        try:
            document = json.loads(intent.body)
        except json.JSONDecodeError as exc:
            raise ReconcileError("managed JSON source is malformed") from exc
        if not isinstance(document, dict) or "_playbook_harness" in document:
            raise ReconcileError("managed JSON source must be an unmarked object")
        return json.dumps(
            {"_playbook_harness": metadata, **document},
            indent=2,
            ensure_ascii=False,
        ) + "\n"
    encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    if intent.marker_style in ("markdown", "markdown-frontmatter"):
        marker = f"<!-- playbook-managed: {encoded} -->"
    elif intent.marker_style == "slash":
        marker = f"// playbook-managed: {encoded}"
    elif intent.marker_style == "hash":
        marker = f"# playbook-managed: {encoded}"
    else:
        raise ReconcileError(f"unknown managed marker style: {intent.marker_style}")
    if intent.marker_style == "markdown-frontmatter" and intent.body.startswith("---\n"):
        closing = intent.body.find("\n---\n", 4)
        if closing < 0:
            raise ReconcileError("managed Markdown frontmatter is malformed")
        insertion = closing + len("\n---\n")
        return intent.body[:insertion] + marker + "\n" + intent.body[insertion:]
    return marker + "\n" + intent.body


def parse_managed_file(text: str, marker_style: str) -> tuple[dict[str, Any], str] | None:
    """Parse a managed file or return None for an unmarked foreign file."""
    if not text:
        return None
    if marker_style == "markdown-frontmatter":
        marker_start = 0
        if text.startswith("---\n"):
            closing = text.find("\n---\n", 4)
            if closing < 0:
                raise ReconcileError("managed Markdown frontmatter is malformed")
            marker_start = closing + len("\n---\n")
        marker_end = text.find("\n", marker_start)
        line = text[marker_start:marker_end] if marker_end >= 0 else text[marker_start:]
        prefix = "<!-- playbook-managed: "
        suffix = " -->"
        if not line.startswith(prefix):
            return None
        if not line.endswith(suffix):
            raise ReconcileError("malformed managed-file marker")
        try:
            metadata = json.loads(line[len(prefix) : -len(suffix)])
        except json.JSONDecodeError as exc:
            raise ReconcileError("malformed managed-file marker") from exc
        if marker_end < 0 or not isinstance(metadata, dict):
            raise ReconcileError("malformed managed-file marker")
        body = text[:marker_start] + text[marker_end + 1 :]
        return metadata, body
    if marker_style == "json":
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ReconcileError("managed JSON file is malformed") from exc
        if not isinstance(document, dict):
            raise ReconcileError("managed JSON root is not an object")
        metadata = document.pop("_playbook_harness", None)
        if metadata is None:
            return None
        if not isinstance(metadata, dict):
            raise ReconcileError("managed JSON marker is malformed")
        body = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        return metadata, body
    first, separator, body = text.partition("\n")
    wrappers = {
        "markdown": ("<!-- playbook-managed: ", " -->"),
        "slash": ("// playbook-managed: ", ""),
        "hash": ("# playbook-managed: ", ""),
    }
    try:
        prefix, suffix = wrappers[marker_style]
    except KeyError as exc:
        raise ReconcileError(f"unknown managed marker style: {marker_style}") from exc
    if not first.startswith(prefix):
        return None
    if suffix and not first.endswith(suffix):
        raise ReconcileError("malformed managed-file marker")
    payload = first[len(prefix) : len(first) - len(suffix) if suffix else None]
    try:
        metadata = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ReconcileError("malformed managed-file marker") from exc
    if not separator or not isinstance(metadata, dict):
        raise ReconcileError("malformed managed-file marker")
    return metadata, body


def render_shared_block(intent: SharedBlockIntent) -> str:
    metadata = {
        "managed_by": MANAGED_BY,
        "schema": MANAGED_SCHEMA,
        "source": intent.source,
        "source_hash": _digest(intent.body),
    }
    encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    return (
        f"# >>> playbook-harness managed {encoded}\n"
        f"{intent.body}"
        f"# <<< playbook-harness managed\n"
    )


def _plan_intent(
    root: Path, intent: Intent, owners: tuple[str, ...]
) -> tuple[PlannedOperation | None, PlanConflict | None]:
    target = validate_relative_target(root, intent.relative)
    if target.is_symlink():
        return None, PlanConflict(intent.relative, "target is a symlink", owners)

    if isinstance(intent, DirectoryIntent):
        if target.exists() and not target.is_dir():
            return None, PlanConflict(intent.relative, "directory path is occupied", owners)
        state = OperationState.UNCHANGED if target.is_dir() else OperationState.CREATE
        return PlannedOperation(intent.relative, state, "directory", owners, mode=intent.mode), None

    if target.exists() and not target.is_file():
        return None, PlanConflict(intent.relative, "file path is occupied", owners)

    if isinstance(intent, ManagedFileIntent):
        desired = render_managed_file(intent)
        if not target.exists():
            return _file_operation(intent, owners, OperationState.CREATE, desired), None
        existing = target.read_text(encoding="utf-8")
        try:
            parsed = parse_managed_file(existing, intent.marker_style)
        except ReconcileError as exc:
            return None, PlanConflict(intent.relative, str(exc), owners)
        if parsed is None:
            if _digest(existing) in intent.adopt_hashes:
                return _file_operation(
                    intent,
                    owners,
                    OperationState.UPDATE,
                    desired,
                    expected_digest=_digest(existing),
                ), None
            return None, PlanConflict(intent.relative, "unmarked file occupies managed path", owners)
        metadata, body = parsed
        if (
            metadata.get("managed_by") != MANAGED_BY
            or metadata.get("schema") != MANAGED_SCHEMA
            or metadata.get("source") != intent.source
        ):
            return None, PlanConflict(intent.relative, "managed marker ownership mismatch", owners)
        if metadata.get("source_hash") != _managed_body_digest(
            body, intent.marker_style
        ):
            return None, PlanConflict(intent.relative, "managed file was locally modified", owners)
        mode_matches = (target.stat().st_mode & 0o777) == intent.mode
        state = (
            OperationState.UNCHANGED
            if existing == desired and mode_matches
            else OperationState.UPDATE
        )
        return _file_operation(
            intent, owners, state, desired, expected_digest=_digest(existing)
        ), None

    if isinstance(intent, CreateOnlyFileIntent):
        if target.exists():
            return _file_operation(intent, owners, OperationState.UNCHANGED, None), None
        return _file_operation(intent, owners, OperationState.CREATE, intent.content), None

    if isinstance(intent, ExactFileIntent):
        if not target.exists():
            return None, PlanConflict(
                intent.relative, "authenticated migration source is missing", owners
            )
        existing = target.read_text(encoding="utf-8")
        digest = _digest(existing)
        if digest != intent.expected_digest:
            return None, PlanConflict(
                intent.relative, "authenticated migration source changed", owners
            )
        return _file_operation(
            intent,
            owners,
            OperationState.UPDATE,
            intent.content,
            expected_digest=digest,
            mode=intent.mode,
        ), None

    if isinstance(intent, ExactDeleteIntent):
        if not target.exists():
            return None, PlanConflict(
                intent.relative, "authenticated deletion source is missing", owners
            )
        existing = target.read_text(encoding="utf-8")
        digest = _digest(existing)
        if digest != intent.expected_digest:
            return None, PlanConflict(
                intent.relative, "authenticated deletion source changed", owners
            )
        if target.stat().st_mode & 0o777 != intent.expected_mode:
            return None, PlanConflict(
                intent.relative, "authenticated deletion source mode changed", owners
            )
        return PlannedOperation(
            intent.relative,
            OperationState.DELETE,
            "file",
            owners,
            mode=intent.expected_mode,
            expected_digest=digest,
        ), None

    if isinstance(intent, SharedBlockIntent):
        if not target.exists():
            return _file_operation(
                intent, owners, OperationState.CREATE, render_shared_block(intent)
            ), None
        existing = target.read_text(encoding="utf-8")
        existing_mode = target.stat().st_mode & 0o777
        try:
            merged = _merge_shared_block(existing, intent)
        except ReconcileError as exc:
            return None, PlanConflict(intent.relative, str(exc), owners)
        state = OperationState.UNCHANGED if merged == existing else OperationState.UPDATE
        return _file_operation(
            intent,
            owners,
            state,
            merged,
            expected_digest=_digest(existing),
            mode=existing_mode,
        ), None

    if isinstance(intent, SharedJsonIntent):
        try:
            current = (
                json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
            )
        except json.JSONDecodeError:
            return None, PlanConflict(intent.relative, "shared JSON is malformed", owners)
        if not isinstance(current, dict):
            return None, PlanConflict(intent.relative, "shared JSON root is not an object", owners)
        merged = json.loads(json.dumps(current))
        try:
            changed = _merge_json_entries(merged, intent.entries)
        except ReconcileError as exc:
            return None, PlanConflict(intent.relative, str(exc), owners)
        content = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
        existing = target.read_text(encoding="utf-8") if target.exists() else None
        existing_mode = target.stat().st_mode & 0o777 if target.exists() else intent.mode
        state = (
            OperationState.CREATE
            if existing is None
            else OperationState.UPDATE
            if changed
            else OperationState.UNCHANGED
        )
        return _file_operation(
            intent,
            owners,
            state,
            content if state != OperationState.UNCHANGED else None,
            expected_digest=_digest(existing) if existing is not None else None,
            mode=existing_mode,
        ), None

    if isinstance(intent, SharedKeyedListIntent):
        try:
            current = (
                json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
            )
        except json.JSONDecodeError:
            return None, PlanConflict(intent.relative, "shared JSON is malformed", owners)
        if not isinstance(current, dict):
            return None, PlanConflict(intent.relative, "shared JSON root is not an object", owners)
        merged = json.loads(json.dumps(current))
        try:
            changed = _merge_keyed_list_entries(merged, intent.entries)
        except ReconcileError as exc:
            return None, PlanConflict(intent.relative, str(exc), owners)
        content = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
        existing = target.read_text(encoding="utf-8") if target.exists() else None
        existing_mode = target.stat().st_mode & 0o777 if target.exists() else intent.mode
        state = (
            OperationState.CREATE
            if existing is None
            else OperationState.UPDATE
            if changed
            else OperationState.UNCHANGED
        )
        return _file_operation(
            intent,
            owners,
            state,
            content if state != OperationState.UNCHANGED else None,
            expected_digest=_digest(existing) if existing is not None else None,
            mode=existing_mode,
        ), None

    raise AssertionError(f"unhandled reconciliation intent: {intent!r}")


def _file_operation(
    intent: Intent,
    owners: tuple[str, ...],
    state: OperationState,
    content: str | None,
    *,
    expected_digest: str | None = None,
    mode: int | None = None,
) -> PlannedOperation:
    return PlannedOperation(
        intent.relative,
        state,
        "file",
        owners,
        content,
        intent.mode if mode is None else mode,
        expected_digest,
    )


def _merge_shared_block(existing: str, intent: SharedBlockIntent) -> str:
    begin = "# >>> playbook-harness managed "
    end = "# <<< playbook-harness managed\n"
    starts = [index for index in range(len(existing)) if existing.startswith(begin, index)]
    ends = [index for index in range(len(existing)) if existing.startswith(end, index)]
    if not starts and not ends:
        separator = "" if not existing or existing.endswith("\n") else "\n"
        return existing + separator + render_shared_block(intent)
    if len(starts) != 1 or len(ends) != 1 or ends[0] <= starts[0]:
        raise ReconcileError("shared managed block is malformed or duplicated")
    marker_end = existing.find("\n", starts[0])
    if marker_end < 0 or marker_end >= ends[0]:
        raise ReconcileError("shared managed block is malformed")
    marker = existing[starts[0] + len(begin) : marker_end]
    try:
        metadata = json.loads(marker)
    except json.JSONDecodeError as exc:
        raise ReconcileError("shared managed block marker is malformed") from exc
    body = existing[marker_end + 1 : ends[0]]
    if (
        metadata.get("managed_by") != MANAGED_BY
        or metadata.get("schema") != MANAGED_SCHEMA
        or metadata.get("source") != intent.source
    ):
        raise ReconcileError("shared managed block ownership mismatch")
    if metadata.get("source_hash") != _digest(body):
        raise ReconcileError("shared managed block was locally modified")
    finish = ends[0] + len(end)
    return existing[: starts[0]] + render_shared_block(intent) + existing[finish:]


def _merge_json_entries(document: dict[str, Any], entries: tuple[JsonEntry, ...]) -> bool:
    changed = False
    seen: dict[tuple[str, ...], tuple[Any, set[str]]] = {}
    for entry in sorted(entries, key=lambda item: item.path):
        if not entry.path or any(not isinstance(part, str) or not part for part in entry.path):
            raise ReconcileError("shared JSON entry has an invalid path")
        if any(re.fullmatch(r"[0-9a-f]{64}", digest) is None for digest in entry.adopt_hashes):
            raise ReconcileError(
                f"shared JSON entry has an invalid owned-value hash: {'.'.join(entry.path)}"
            )
        previous = seen.get(entry.path)
        if previous is not None and previous[0] != entry.value:
            raise ReconcileError("shared JSON contribution contains incompatible duplicates")
        hashes = previous[1] if previous is not None else set()
        hashes.update(entry.adopt_hashes)
        seen[entry.path] = (entry.value, hashes)

    for path, (value, adopt_hashes) in sorted(seen.items()):
        parent: dict[str, Any] = document
        for part in path[:-1]:
            child = parent.get(part)
            if child is None:
                child = {}
                parent[part] = child
                changed = True
            if not isinstance(child, dict):
                raise ReconcileError(
                    f"shared JSON path collides with non-object: {'.'.join(path)}"
                )
            parent = child
        leaf = path[-1]
        if leaf not in parent:
            parent[leaf] = value
            changed = True
        elif parent[leaf] != value:
            if json_value_digest(parent[leaf]) in adopt_hashes:
                parent[leaf] = value
                changed = True
            else:
                raise ReconcileError(
                    f"shared JSON entry is user-owned or conflicting: {'.'.join(path)}"
                )
    return changed


def _merge_keyed_list_entries(
    document: dict[str, Any], entries: tuple[KeyedListEntry, ...]
) -> bool:
    changed = False
    grouped: dict[tuple[Any, ...], KeyedListEntry] = {}
    for entry in entries:
        if (
            not entry.path
            or not entry.key_fields
            or any(not part for part in (*entry.path, *entry.key_fields))
        ):
            raise ReconcileError("shared keyed-list entry has an invalid path or key")
        if bool(entry.nested_list_field) != bool(entry.nested_key_fields):
            raise ReconcileError("shared keyed-list nested ownership is incomplete")
        if any(re.fullmatch(r"[0-9a-f]{64}", item) is None for item in entry.adopt_hashes):
            raise ReconcileError("shared keyed-list entry has an invalid owned-value hash")
        if any(len(alias) != len(entry.key_fields) for alias in entry.adopt_keys):
            raise ReconcileError("shared keyed-list entry has an invalid adopted key")
        key = tuple(
            json.dumps(
                entry.value.get(field),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            for field in entry.key_fields
        )
        identity = (
            entry.path,
            entry.key_fields,
            key,
            entry.nested_list_field,
            entry.nested_key_fields,
        )
        previous = grouped.get(identity)
        if previous is not None and previous.value != entry.value:
            raise ReconcileError("shared keyed-list contribution contains incompatible duplicates")
        grouped[identity] = KeyedListEntry(
            entry.path,
            entry.key_fields,
            entry.value,
            tuple(sorted(set(entry.adopt_hashes) | set(previous.adopt_hashes if previous else ()))),
            entry.nested_list_field,
            entry.nested_key_fields,
            tuple(
                sorted(
                    set(entry.adopt_keys)
                    | set(previous.adopt_keys if previous else ()),
                    key=repr,
                )
            ),
        )

    for identity, entry in sorted(grouped.items(), key=lambda item: repr(item[0])):
        path, key_fields, key, nested_list_field, nested_key_fields = identity
        parent: dict[str, Any] = document
        for part in path[:-1]:
            child = parent.get(part)
            if child is None:
                child = {}
                parent[part] = child
                changed = True
            if not isinstance(child, dict):
                raise ReconcileError(f"shared keyed-list path collides: {'.'.join(path)}")
            parent = child
        values = parent.get(path[-1])
        if values is None:
            values = []
            parent[path[-1]] = values
            changed = True
        if not isinstance(values, list):
            raise ReconcileError(f"shared keyed-list target is not a list: {'.'.join(path)}")
        accepted_keys = {key}
        accepted_keys.update(
            tuple(
                json.dumps(
                    value,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                for value in alias
            )
            for alias in entry.adopt_keys
        )
        matches = [
            (index, value) for index, value in enumerate(values)
            if isinstance(value, dict)
            and tuple(
                json.dumps(
                    value.get(field),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                for field in key_fields
            ) in accepted_keys
        ]
        if nested_list_field:
            desired_children = entry.value.get(nested_list_field)
            if (
                not isinstance(desired_children, list)
                or len(desired_children) != 1
                or not isinstance(desired_children[0], dict)
            ):
                raise ReconcileError("shared keyed-list nested value must contain one object")
            desired_child = desired_children[0]
            nested_key = tuple(
                json.dumps(
                    desired_child.get(field),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                for field in nested_key_fields
            )
            if not matches:
                values.append(entry.value)
                changed = True
                continue

            owned_outer_matches = []
            for index, value in matches:
                children = value.get(nested_list_field)
                if not isinstance(children, list) or not all(
                    isinstance(child, dict) for child in children
                ):
                    raise ReconcileError(
                        f"shared keyed-list nested target is not an object list: {'.'.join(path)}"
                    )
                owns = any(
                    all(
                        json.dumps(
                            child.get(field),
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ) == expected
                        for field, expected in zip(nested_key_fields, nested_key)
                    )
                    or json_value_digest(child) in entry.adopt_hashes
                    for child in children
                )
                if owns:
                    owned_outer_matches.append((index, value))
            desired_matches = [
                (index, value)
                for index, value in matches
                if tuple(
                    json.dumps(
                        value.get(field),
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                    for field in key_fields
                ) == key
            ]
            eligible_matches = owned_outer_matches or desired_matches
            if not eligible_matches:
                values.append(entry.value)
                changed = True
                continue
            if len(eligible_matches) > 1:
                raise ReconcileError(
                    f"shared keyed-list nested ownership is ambiguous: {'.'.join(path)}"
                )
            _, target_outer = eligible_matches[0]
            children = target_outer[nested_list_field]
            child_matches = [
                (index, child)
                for index, child in enumerate(children)
                if all(
                    json.dumps(
                        child.get(field),
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ) == expected
                    for field, expected in zip(nested_key_fields, nested_key)
                )
                or json_value_digest(child) in entry.adopt_hashes
            ]
            if not child_matches:
                children.append(desired_child)
                changed = True
            else:
                first = child_matches[0][0]
                duplicate_indexes = {index for index, _ in child_matches[1:]}
                if duplicate_indexes:
                    children[:] = [
                        child for index, child in enumerate(children)
                        if index not in duplicate_indexes
                    ]
                    changed = True
                if children[first] != desired_child:
                    children[first] = desired_child
                    changed = True
            for field in key_fields:
                if target_outer.get(field) != entry.value.get(field):
                    target_outer[field] = entry.value.get(field)
                    changed = True
            continue
        if not matches:
            values.append(entry.value)
            changed = True
        elif all(value == entry.value for _, value in matches):
            if len(matches) > 1:
                duplicate_indexes = {index for index, _ in matches[1:]}
                values[:] = [value for index, value in enumerate(values) if index not in duplicate_indexes]
                changed = True
        elif (
            all(value == matches[0][1] for _, value in matches)
            and json_value_digest(matches[0][1]) in entry.adopt_hashes
        ):
            first = matches[0][0]
            duplicate_indexes = {index for index, _ in matches[1:]}
            values[:] = [value for index, value in enumerate(values) if index not in duplicate_indexes]
            values[first] = entry.value
            changed = True
        elif len(matches) == 1 and json_value_digest(matches[0][1]) in entry.adopt_hashes:
            values[matches[0][0]] = entry.value
            changed = True
        else:
            raise ReconcileError(
                f"shared keyed-list entry is user-owned or ambiguous: {'.'.join(path)}"
            )
    return changed


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _managed_body_digest(body: str, marker_style: str) -> str:
    if marker_style != "json":
        return _digest(body)
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ReconcileError("managed JSON source is malformed") from exc
    return json_value_digest(value)


def json_value_digest(value: Any) -> str:
    """Hash one canonical JSON value for explicit prior-owned-value adoption."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _digest(encoded)


def apply_reconciliation(
    plan: ReconciliationPlan,
    *,
    fault: FaultHook | None = None,
) -> ApplyResult:
    """Apply a conflict-free plan as one transaction with grouped reporting.

    Predictable conflicts and stale plans are rejected before any mutation.
    Any caught failure rolls back every already-published operation in the
    plan; group names remain a reporting concern, not commit boundaries.
    """
    if plan.conflicts:
        summary = "; ".join(
            f"{conflict.relative}: {conflict.reason}" for conflict in plan.conflicts
        )
        raise ApplyFailure("preflight", plan.conflicts[0].relative, summary)
    _preflight_apply(plan)
    _cleanup_stale_temporaries(plan)

    grouped: dict[str, list[PlannedOperation]] = {}
    for operation in plan.operations:
        grouped.setdefault(_operation_group(operation), []).append(operation)
    order = sorted(grouped, key=lambda name: (name != "shared", name))

    ordered_operations = [
        operation for group in order for operation in grouped[group]
    ]
    changed = frozenset(
        _apply_group(plan.root, "plan", ordered_operations, fault=fault)
    )
    results = [
        AppliedGroup(
            group,
            tuple(
                operation.relative
                for operation in grouped[group]
                if operation.relative in changed
            ),
        )
        for group in order
    ]
    return ApplyResult(tuple(results))


def _preflight_apply(plan: ReconciliationPlan) -> None:
    if not plan.root.matches():
        raise ApplyFailure("preflight", ".", "canonical project root identity changed")
    for operation in plan.operations:
        target = validate_relative_target(plan.root.path, operation.relative)
        if operation.kind == "directory":
            if operation.state == OperationState.CREATE and target.exists():
                raise ApplyFailure("preflight", operation.relative, "planned directory now exists")
            if operation.state == OperationState.UNCHANGED and not target.is_dir():
                raise ApplyFailure("preflight", operation.relative, "planned directory changed")
            continue
        if operation.state == OperationState.CREATE:
            if target.exists() or target.is_symlink():
                raise ApplyFailure("preflight", operation.relative, "planned file path is now occupied")
        elif operation.state in (OperationState.UPDATE, OperationState.DELETE):
            if not target.is_file() or target.is_symlink():
                raise ApplyFailure("preflight", operation.relative, "planned file changed type")
            existing = target.read_text(encoding="utf-8")
            if operation.expected_digest != _digest(existing):
                raise ApplyFailure("preflight", operation.relative, "planned file content changed")
            if (
                operation.state == OperationState.DELETE
                and target.stat().st_mode & 0o777 != operation.mode
            ):
                raise ApplyFailure("preflight", operation.relative, "planned file mode changed")
        elif operation.expected_digest is not None:
            if not target.is_file() or target.is_symlink():
                raise ApplyFailure("preflight", operation.relative, "unchanged file changed type")
            if operation.expected_digest != _digest(target.read_text(encoding="utf-8")):
                raise ApplyFailure("preflight", operation.relative, "unchanged file content changed")


def _validate_operation_preimage(
    identity: RootIdentity, operation: PlannedOperation
) -> Path:
    target = _revalidate(identity, operation.relative)
    if operation.state == OperationState.CREATE:
        if target.exists() or target.is_symlink():
            raise ReconcileError("planned file path is now occupied")
    elif operation.state in (OperationState.UPDATE, OperationState.DELETE):
        if not target.is_file() or target.is_symlink():
            raise ReconcileError("planned file changed type")
        if operation.expected_digest != _digest(target.read_text(encoding="utf-8")):
            raise ReconcileError("planned file content changed")
        if (
            operation.state == OperationState.DELETE
            and target.stat().st_mode & 0o777 != operation.mode
        ):
            raise ReconcileError("planned file mode changed")
    return target


def _validate_managed_postimage(target: Path, operation: PlannedOperation) -> None:
    if operation.state == OperationState.DELETE:
        if target.exists() or target.is_symlink():
            raise ReconcileError("deleted postimage reappeared")
        return
    if target.is_symlink() or not target.is_file():
        raise ReconcileError("managed postimage changed type")
    expected = (operation.content or "").encode("utf-8")
    if target.read_bytes() != expected:
        raise ReconcileError("managed postimage changed")
    if target.stat().st_mode & 0o777 != operation.mode:
        raise ReconcileError("managed postimage mode changed")


def _operation_group(operation: PlannedOperation) -> str:
    if "shared" in operation.owners or len(operation.owners) != 1:
        return "shared"
    return operation.owners[0]


def _apply_group(
    identity: RootIdentity,
    group: str,
    operations: list[PlannedOperation],
    *,
    fault: FaultHook | None,
) -> list[str]:
    mutable = [
        operation
        for operation in operations
        if operation.state != OperationState.UNCHANGED
    ]
    if not mutable:
        return []

    created_dirs: list[Path] = []
    stages: dict[str, tuple[Path, tuple[int, int]]] = {}
    originals: dict[str, tuple[bytes, int] | None] = {}
    committed: list[PlannedOperation] = []
    current = mutable[0]
    try:
        directories = _required_directories(identity.path, mutable)
        for directory, mode in directories:
            if directory.exists():
                continue
            _inject(fault, "before_mkdir", directory)
            _revalidate(identity, directory.relative_to(identity.path))
            directory.mkdir(mode=mode)
            created_dirs.append(directory)

        for operation in mutable:
            current = operation
            if operation.kind == "directory":
                continue
            target = _validate_operation_preimage(identity, operation)
            _inject(fault, "before_stage", target)
            target = _validate_operation_preimage(identity, operation)
            if operation.state in (OperationState.UPDATE, OperationState.DELETE):
                originals[operation.relative] = (
                    target.read_bytes(),
                    target.stat().st_mode & 0o777,
                )
            else:
                originals[operation.relative] = None
            if operation.state != OperationState.DELETE:
                stages[operation.relative] = _stage_file(
                    target, operation.content or "", operation.mode
                )

        for operation in mutable:
            current = operation
            if operation.kind == "directory":
                continue
            target = _validate_operation_preimage(identity, operation)
            _inject(fault, "before_replace", target)
            target = _validate_operation_preimage(identity, operation)
            if operation.state == OperationState.DELETE:
                target.unlink()
                _fsync_directory(target.parent)
                committed.append(operation)
                continue
            stage, parent_identity = stages[operation.relative]
            if not _directory_matches(target.parent, parent_identity):
                raise ReconcileError(f"target parent identity changed: {target.parent}")
            os.replace(stage, target)
            _fsync_directory(target.parent)
            committed.append(operation)
        return [operation.relative for operation in mutable]
    except BaseException as exc:
        rollback_errors: list[str] = []
        for operation in reversed(committed):
            target = identity.path / operation.relative
            try:
                _inject(fault, "before_restore", target)
                target = _revalidate(identity, operation.relative)
                _validate_managed_postimage(target, operation)
                original = originals[operation.relative]
                if original is None:
                    target.unlink(missing_ok=True)
                    _fsync_directory(target.parent)
                else:
                    body, mode = original
                    _restore_file(target, body, mode)
            except BaseException as rollback_exc:
                rollback_errors.append(f"{operation.relative}: {rollback_exc}")
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        detail = str(exc)
        if rollback_errors:
            detail += "; rollback incomplete: " + "; ".join(rollback_errors)
        failure_group = _operation_group(current) if group == "plan" else group
        raise ApplyFailure(failure_group, current.relative, detail) from exc
    finally:
        for stage, parent_identity in stages.values():
            _safe_unlink(stage, parent_identity)


def _required_directories(
    root: Path, operations: list[PlannedOperation]
) -> list[tuple[Path, int]]:
    modes: dict[Path, int] = {}
    for operation in operations:
        target = root / operation.relative
        if operation.kind == "directory":
            modes[target] = operation.mode
            parent = target.parent
        else:
            if operation.state == OperationState.DELETE:
                continue
            parent = target.parent
        while parent != root:
            modes.setdefault(parent, 0o755)
            parent = parent.parent
    return sorted(modes.items(), key=lambda item: (len(item[0].parts), str(item[0])))


def _stage_file(target: Path, content: str, mode: int) -> tuple[Path, tuple[int, int]]:
    parent_stat = target.parent.stat()
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    descriptor, name = tempfile.mkstemp(
        prefix=f".playbook-reconcile-stage-p{os.getpid()}-{digest}-",
        dir=target.parent,
    )
    stage = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        stage.chmod(mode)
    except BaseException:
        _safe_unlink(stage, (parent_stat.st_dev, parent_stat.st_ino))
        raise
    return stage, (parent_stat.st_dev, parent_stat.st_ino)


def _restore_file(target: Path, body: bytes, mode: int) -> None:
    digest = hashlib.sha256(body).hexdigest()
    descriptor, name = tempfile.mkstemp(
        prefix=f".playbook-reconcile-restore-p{os.getpid()}-{digest}-",
        dir=target.parent,
    )
    temporary = Path(name)
    parent_stat = target.parent.stat()
    parent_identity = (parent_stat.st_dev, parent_stat.st_ino)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        _safe_unlink(temporary, parent_identity)


def _revalidate(identity: RootIdentity, relative: PurePath | str) -> Path:
    if not identity.matches():
        raise ReconcileError("canonical project root identity changed")
    return validate_relative_target(identity.path, relative)


def _directory_matches(path: Path, expected: tuple[int, int]) -> bool:
    if path.is_symlink():
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    return path.is_dir() and (stat.st_dev, stat.st_ino) == expected


def _safe_unlink(path: Path, parent_identity: tuple[int, int]) -> None:
    if _directory_matches(path.parent, parent_identity):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


_TEMPORARY_RE = re.compile(
    r"^\.playbook-reconcile-(?:stage|restore)-p([0-9]+)-([0-9a-f]{64})-[A-Za-z0-9_]+$"
)


def _cleanup_stale_temporaries(plan: ReconciliationPlan) -> None:
    """Remove only dead-process temporaries whose content authenticates the name."""
    parents: set[Path] = set()
    for operation in plan.operations:
        if operation.kind != "file":
            continue
        target = validate_relative_target(plan.root.path, operation.relative)
        if target.parent.is_dir() and not target.parent.is_symlink():
            parents.add(target.parent)

    for parent in sorted(parents):
        parent_stat = parent.stat()
        parent_identity = (parent_stat.st_dev, parent_stat.st_ino)
        for candidate in parent.iterdir():
            match = _TEMPORARY_RE.fullmatch(candidate.name)
            if match is None or candidate.is_symlink() or not candidate.is_file():
                continue
            pid = int(match.group(1))
            if pid == os.getpid() or _pid_alive(pid):
                continue
            stat = candidate.stat()
            if hasattr(os, "getuid") and stat.st_uid != os.getuid():
                continue
            if hashlib.sha256(candidate.read_bytes()).hexdigest() != match.group(2):
                continue
            _safe_unlink(candidate, parent_identity)
            _fsync_directory(parent)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _inject(fault: FaultHook | None, event: str, path: Path) -> None:
    if fault is not None:
        fault(event, path)


def _runtime_asset(relative: str) -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / relative
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def _validated_agent_dir(root: Path) -> Path:
    agent_root = root / ".agent"
    marker = agent_root / "current_user"
    if agent_root.is_symlink():
        raise ReconcileError(f"project agent directory may not be a symlink: {agent_root}")
    if marker.is_symlink():
        raise ReconcileError(f"current-user marker may not be a symlink: {marker}")
    try:
        agent_dir = resolve_agent_dir(root)
    except SystemExit as exc:
        raise ReconcileError(f"invalid current-user marker: {marker}") from exc
    if agent_dir != agent_root and agent_dir.is_symlink():
        raise ReconcileError(f"selected agent directory may not be a symlink: {agent_dir}")
    return agent_dir
