"""Strict, preservation-oriented access to authority fields in ``task.md``."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import os
from pathlib import Path
import re
import tempfile
from typing import Iterator

from provider.session_state import SessionKey


class TaskDocumentError(ValueError):
    """The managed task authority fields are absent, ambiguous, or malformed."""


class TaskClaimConflict(TaskDocumentError):
    """An open task is owned by another native provider conversation."""

    def __init__(self, owner: SessionKey):
        self.owner = owner
        super().__init__(
            f"task is already claimed by {owner.provider}:{owner.session_id}"
        )


class TaskClaimCASMismatch(TaskDocumentError):
    """An explicit transfer named an owner that is no longer authoritative."""


class TaskAuthorityMismatch(TaskDocumentError):
    """A session navigation cache disagrees with task.md ownership."""


_STATUS_ALIASES = {
    "pending": "pending",
    "in_progress": "in_progress",
    "in-progress": "in_progress",
    "done": "done",
    "complete": "done",
    "completed": "done",
}
_HEADING = re.compile(r"^##[ \t]+(.+?)[ \t]*(?:\r?\n)?$")
_FENCE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})")
_SESSION = re.compile(r"^[ \t]*-[ \t]+([a-zA-Z0-9_-]+):([^\s]+)[ \t]*(?:\r?\n)?$")
_OPAQUE_START = "<!-- playbook-recent-chat:start -->"
_OPAQUE_END = "<!-- playbook-recent-chat:end -->"
_GATE = re.compile(r"^[ \t]*-[ \t]+\[([ xX])\][ \t]*(.*?)[ \t]*(?:\r?\n)?$")


@dataclass(frozen=True)
class _Section:
    heading: int
    end: int


@dataclass(frozen=True)
class TaskGate:
    """One executable checkbox outside fenced and captured-chat regions."""

    line: int
    checked: bool
    text: str


@dataclass(frozen=True)
class TaskGateClosure:
    """One gate changed from open to checked by a committed mediated edit."""

    line: int
    before: str
    after: str


@dataclass(frozen=True)
class TaskDocument:
    """A parsed task whose managed fields can be rendered without a rewrite."""

    lines: tuple[str, ...]
    status: str
    sessions: tuple[SessionKey, ...]
    _status_section: _Section
    _status_value_line: int
    _sessions_section: _Section | None
    _newline: str
    _semantic: tuple[bool, ...]

    @property
    def live_owner(self) -> SessionKey | None:
        """Return the final provenance entry only while the task is claimed."""
        return _live_owner(self)

    @property
    def gates(self) -> tuple[TaskGate, ...]:
        gates = []
        for index, line in enumerate(self.lines):
            if not self._semantic[index]:
                continue
            match = _GATE.match(line)
            if match:
                gates.append(
                    TaskGate(index, match.group(1).lower() == "x", match.group(2).strip())
                )
        return tuple(gates)

    @property
    def head_position(self) -> str:
        """Return the first semantic open gate or empty required field."""
        for index, line in enumerate(self.lines):
            if not self._semantic[index]:
                continue
            match = _GATE.match(line)
            if match and match.group(1) == " ":
                return match.group(2).strip()
            stripped = line.strip()
            if stripped.endswith(":") and stripped.startswith("- **"):
                return stripped
        return "(all gates checked)"

    @property
    def progress(self) -> tuple[int, int]:
        gates = self.gates
        return sum(gate.checked for gate in gates), len(gates)

    def section_span(self, title: str) -> tuple[int, int] | None:
        """Return semantic line bounds for one unambiguous H2 section."""
        headings = []
        for index, line in enumerate(self.lines):
            if not self._semantic[index]:
                continue
            match = _HEADING.match(line)
            if match:
                headings.append((index, match.group(1).strip()))
        matches = [pos for pos, (_, name) in enumerate(headings) if name == title]
        if len(matches) > 1:
            raise TaskDocumentError(f"task.md contains duplicate ## {title} headings")
        if not matches:
            return None
        position = matches[0]
        start = headings[position][0]
        end = headings[position + 1][0] if position + 1 < len(headings) else len(self.lines)
        return start, end

    @classmethod
    def parse(cls, text: str) -> "TaskDocument":
        lines = tuple(text.splitlines(keepends=True))
        headings: list[tuple[int, str]] = []
        semantic = [True] * len(lines)
        fence_char: str | None = None
        fence_width = 0
        opaque = False
        for index, line in enumerate(lines):
            if line.strip() == _OPAQUE_START:
                opaque = True
                semantic[index] = False
                continue
            if opaque:
                semantic[index] = False
                if line.strip() == _OPAQUE_END:
                    opaque = False
                continue
            fence = _FENCE.match(line)
            if fence:
                semantic[index] = False
                marker = fence.group(1)
                if fence_char is None:
                    fence_char, fence_width = marker[0], len(marker)
                elif marker[0] == fence_char and len(marker) >= fence_width:
                    fence_char, fence_width = None, 0
                continue
            if fence_char is not None:
                semantic[index] = False
            if fence_char is None:
                heading = _HEADING.match(line)
                if heading:
                    headings.append((index, heading.group(1).strip()))
        if opaque:
            raise TaskDocumentError("task.md contains an unterminated recent-chat region")

        def section(name: str, *, required: bool) -> _Section | None:
            matches = [pos for pos, (_, title) in enumerate(headings) if title == name]
            if len(matches) > 1:
                raise TaskDocumentError(f"task.md contains duplicate ## {name} headings")
            if not matches:
                if required:
                    raise TaskDocumentError(f"task.md is missing ## {name}")
                return None
            heading_pos = matches[0]
            start = headings[heading_pos][0]
            end = headings[heading_pos + 1][0] if heading_pos + 1 < len(headings) else len(lines)
            return _Section(start, end)

        status_section = section("Status", required=True)
        assert status_section is not None
        status_values = [
            index for index in range(status_section.heading + 1, status_section.end)
            if semantic[index] and lines[index].strip()
        ]
        if not status_values:
            raise TaskDocumentError("## Status must contain a status token")
        status_line = status_values[0]
        raw_status = lines[status_line].strip().lower()
        try:
            status = _STATUS_ALIASES[raw_status]
        except KeyError as exc:
            raise TaskDocumentError(f"unsupported task status: {raw_status!r}") from exc

        sessions_section = section("Sessions", required=False)
        sessions: list[SessionKey] = []
        if sessions_section is not None:
            for index in range(sessions_section.heading + 1, sessions_section.end):
                if not semantic[index] or not lines[index].strip():
                    continue
                match = _SESSION.match(lines[index])
                if not match:
                    raise TaskDocumentError(
                        f"malformed ## Sessions entry on line {index + 1}"
                    )
                try:
                    sessions.append(SessionKey.from_values(match.group(1), match.group(2)))
                except (KeyError, ValueError) as exc:
                    raise TaskDocumentError(
                        f"malformed ## Sessions entry on line {index + 1}: {exc}"
                    ) from exc

        return cls(
            lines=lines,
            status=status,
            sessions=tuple(sessions),
            _status_section=status_section,
            _status_value_line=status_line,
            _sessions_section=sessions_section,
            _newline=next((_line_ending(line) for line in lines if _line_ending(line)), "\n"),
            _semantic=tuple(semantic),
        )

    def render(self, *, status: str | None = None,
               append_session: SessionKey | None = None) -> str:
        """Render canonical managed values while preserving all other text."""
        canonical = self.status if status is None else _canonical_status(status)
        lines = list(self.lines)
        newline = _line_ending(lines[self._status_value_line])
        lines[self._status_value_line] = canonical + newline

        if append_session is None or (
            self.sessions and self.sessions[-1] == append_session
        ):
            return "".join(lines)

        entry = f"- {append_session.provider}:{append_session.session_id}{self._newline}"
        if self._sessions_section is not None:
            insert_at = self._sessions_section.end
            while insert_at > self._sessions_section.heading + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines.insert(insert_at, entry)
            return "".join(lines)

        # Insert the managed provenance section immediately after Status,
        # before the next authored H2, retaining that section byte-for-byte.
        insert_at = self._status_section.end
        prefix = "" if insert_at == 0 or not lines[insert_at - 1].strip() else "\n"
        nl = self._newline
        prefix = "" if not prefix else nl
        lines.insert(insert_at, f"{prefix}## Sessions{nl}{nl}{entry}{nl}")
        return "".join(lines)


def update_task_document(task_file: Path, *, status: str | None = None,
                         append_session: SessionKey | None = None) -> TaskDocument:
    """Validate then minimally update one task document."""
    with task_file.open("r", encoding="utf-8", newline="") as stream:
        original = stream.read()
    parsed = TaskDocument.parse(original)
    rendered = parsed.render(status=status, append_session=append_session)
    if rendered != original:
        with task_file.open("w", encoding="utf-8", newline="") as stream:
            stream.write(rendered)
    return TaskDocument.parse(rendered)


def claim_task_document(
    task_file: Path,
    claimant: SessionKey,
    *,
    expected_owner: SessionKey | None = None,
) -> tuple[TaskDocument, bool]:
    """Atomically claim/reopen a task; return the document and whether it changed."""
    with _task_lock(task_file):
        original = _read_exact(task_file)
        document = TaskDocument.parse(original)
        owner = _live_owner(document)
        if owner == claimant:
            if expected_owner is not None and expected_owner != owner:
                # Replay after the task authority committed but a rebuildable
                # cache/old-pointer cleanup was interrupted.
                if len(document.sessions) < 2 or document.sessions[-2] != expected_owner:
                    raise TaskClaimCASMismatch(
                        f"expected owner {expected_owner.provider}:{expected_owner.session_id}, "
                        f"but current owner is {owner.provider}:{owner.session_id}"
                    )
            return document, False
        if owner is not None:
            if expected_owner is None:
                raise TaskClaimConflict(owner)
            if expected_owner != owner:
                raise TaskClaimCASMismatch(
                    f"expected owner {expected_owner.provider}:{expected_owner.session_id}, "
                    f"but current owner is {owner.provider}:{owner.session_id}"
                )
        elif expected_owner is not None:
            raise TaskClaimCASMismatch("task has no current owner")
        rendered = document.render(status="in_progress", append_session=claimant)
        _atomic_replace(task_file, rendered)
        return TaskDocument.parse(rendered), rendered != original


def complete_task_document(task_file: Path, claimant: SessionKey) -> TaskDocument:
    """Atomically close a task only for its authoritative current owner."""
    with _task_lock(task_file):
        original = _read_exact(task_file)
        document = TaskDocument.parse(original)
        if document.status == "done" and document.sessions and document.sessions[-1] == claimant:
            return document
        owner = _live_owner(document)
        if owner != claimant:
            if owner is None:
                raise TaskDocumentError("task is not claimed by any session")
            raise TaskClaimConflict(owner)
        rendered = document.render(status="done")
        _atomic_replace(task_file, rendered)
        return TaskDocument.parse(rendered)


def replace_unclaimed_task_text(
    task_file: Path, old: str, new: str
) -> TaskDocument:
    """CAS an unclaimed task (for stub expansion) under the authority lock."""
    with _task_lock(task_file):
        original = _read_exact(task_file)
        if original != old:
            raise TaskClaimCASMismatch("task changed before unclaimed edit committed")
        before = TaskDocument.parse(original)
        if before.live_owner is not None:
            raise TaskClaimConflict(before.live_owner)
        after = TaskDocument.parse(new)
        if after.status != before.status or after.sessions != before.sessions:
            raise TaskDocumentError(
                "unclaimed edit cannot change ## Status or ## Sessions authority"
            )
        _atomic_replace(task_file, new)
        return after


def validate_task_claim(
    agent_dir: Path, key: SessionKey, task_number: str
) -> Path:
    """Resolve a navigation cache only when task.md confirms the same owner."""
    task_file = resolve_task_document(agent_dir, task_number)
    state_path = agent_dir / "sessions" / key.directory_name / "current_state"
    document = TaskDocument.parse(_read_exact(task_file))
    owner = _live_owner(document)
    if owner != key:
        owner_text = "unclaimed" if owner is None else f"{owner.provider}:{owner.session_id}"
        raise TaskAuthorityMismatch(
            f"navigation cache {state_path} identifies {key.provider}:{key.session_id}, "
            f"but task authority {task_file} identifies {owner_text}"
        )
    return task_file


def resolve_task_document(agent_dir: Path, task_number: str) -> Path:
    """Resolve exactly one numeric task.md without consulting a cache."""
    matches = []
    tasks_dir = agent_dir / "tasks"
    if tasks_dir.exists():
        for child in tasks_dir.iterdir():
            prefix, separator, _ = child.name.partition("-")
            if not separator:
                continue
            try:
                same_number = int(prefix) == int(task_number)
            except ValueError:
                same_number = False
            if same_number and (child / "task.md").is_file():
                matches.append(child / "task.md")
    if len(matches) != 1:
        if not matches:
            raise TaskAuthorityMismatch(f"task {task_number} not found")
        raise TaskAuthorityMismatch(
            f"task number {task_number} resolves to {len(matches)} matching task.md files"
        )
    return matches[0]


def complete_next_gate(
    task_file: Path, claimant: SessionKey
) -> tuple[str, list[str]]:
    """Atomically check exactly one gate on a task owned by ``claimant``."""
    with _task_lock(task_file):
        original = _read_exact(task_file)
        document = TaskDocument.parse(original)
        owner = _live_owner(document)
        if owner != claimant:
            if owner is None:
                raise TaskDocumentError("task is not claimed by any session")
            raise TaskClaimConflict(owner)
        lines = list(document.lines)
        open_gates = [gate for gate in document.gates if not gate.checked]
        if not open_gates:
            raise TaskDocumentError("task has no unchecked gate")
        current = open_gates[0]
        lines[current.line] = lines[current.line].replace("[ ]", "[x]", 1)
        upcoming: list[str] = []
        open_by_line = {gate.line: gate.text for gate in open_gates[1:]}
        for index in range(current.line + 1, len(lines)):
            if index in open_by_line:
                upcoming.append(open_by_line[index])
                continue
            if not document._semantic[index]:
                continue
            stripped = lines[index].strip()
            if stripped.endswith(":") and stripped.startswith("- **"):
                upcoming.append(stripped)
            if len(upcoming) >= 3:
                break
        _atomic_replace(task_file, "".join(lines))
        return current.text, upcoming


def replace_claimed_task_text(
    task_file: Path,
    claimant: SessionKey,
    old: str,
    new: str,
    *,
    replace_all: bool = False,
) -> TaskDocument:
    """Atomically replace authored task text without changing authority fields."""
    document, _closures, _changed = replace_claimed_task_text_with_closures(
        task_file,
        claimant,
        old,
        new,
        replace_all=replace_all,
        recognize_replay=False,
    )
    return document


def _closed_gates(
    before: TaskDocument, after: TaskDocument
) -> tuple[TaskGateClosure, ...]:
    before_by_line = {gate.line: gate for gate in before.gates}
    closures = []
    for gate in after.gates:
        prior = before_by_line.get(gate.line)
        if prior is not None and not prior.checked and gate.checked:
            closures.append(TaskGateClosure(gate.line, prior.text, gate.text))
    return tuple(closures)


def _replacement_gate_closures(
    after: TaskDocument, old: str, new: str
) -> tuple[TaskGateClosure, ...]:
    """Recover gate identity from the mediated replacement even if lines move."""
    old_gates = [_GATE.match(line) for line in old.splitlines()]
    new_gates = [_GATE.match(line) for line in new.splitlines()]
    transitions: list[tuple[str, str]] = []
    for prior, current in zip(
        (match for match in old_gates if match is not None),
        (match for match in new_gates if match is not None),
    ):
        if prior.group(1) == " " and current.group(1).lower() == "x":
            transitions.append((prior.group(2).strip(), current.group(2).strip()))
    closures = []
    for prior_text, current_text in transitions:
        matches = [
            gate for gate in after.gates
            if gate.checked and gate.text == current_text
        ]
        if len(matches) == 1:
            closures.append(
                TaskGateClosure(matches[0].line, prior_text, current_text)
            )
    return tuple(closures)


def replace_claimed_task_text_with_closures(
    task_file: Path,
    claimant: SessionKey,
    old: str,
    new: str,
    *,
    replace_all: bool = False,
    recognize_replay: bool = True,
) -> tuple[TaskDocument, tuple[TaskGateClosure, ...], bool]:
    """Commit one mediated edit and return its gate-close transition evidence."""
    if not old:
        raise TaskDocumentError("task edit old text must be nonempty")
    with _task_lock(task_file):
        original = _read_exact(task_file)
        before = TaskDocument.parse(original)
        owner = _live_owner(before)
        if owner != claimant:
            if owner is None:
                raise TaskDocumentError("task is not claimed by any session")
            raise TaskClaimConflict(owner)
        occurrences = original.count(old)
        if occurrences == 0:
            if recognize_replay and original.count(new) == 1:
                hypothetical = original.replace(new, old, 1)
                replay_before = TaskDocument.parse(hypothetical)
                if (
                    replay_before.status == before.status
                    and replay_before.sessions == before.sessions
                ):
                    closures = _replacement_gate_closures(before, old, new)
                    if not closures:
                        closures = _closed_gates(replay_before, before)
                    if len(closures) == 1:
                        return before, closures, False
            raise TaskDocumentError("task edit old text was not found")
        if occurrences > 1 and not replace_all:
            raise TaskDocumentError(
                f"task edit old text is ambiguous ({occurrences} occurrences)"
            )
        candidate = original.replace(old, new, -1 if replace_all else 1)
        after = TaskDocument.parse(candidate)
        if after.status != before.status or after.sessions != before.sessions:
            raise TaskDocumentError(
                "task edit cannot change ## Status or ## Sessions authority"
            )
        closures = _replacement_gate_closures(after, old, new)
        if not closures:
            closures = _closed_gates(before, after)
        if after.progress[0] - before.progress[0] > 1 or len(closures) > 1:
            raise TaskDocumentError("one task edit cannot close multiple gates")
        _atomic_replace(task_file, candidate)
        return after, closures, True


@contextmanager
def task_authority_lock(task_file: Path) -> Iterator[None]:
    """Serialize an external task-control transaction with lifecycle writes."""
    with _task_lock(task_file):
        yield


def _live_owner(document: TaskDocument) -> SessionKey | None:
    if document.status != "in_progress":
        return None
    if not document.sessions:
        raise TaskDocumentError("in_progress task has no ## Sessions owner")
    return document.sessions[-1]


@contextmanager
def _task_lock(task_file: Path) -> Iterator[None]:
    if task_file.is_symlink() or task_file.parent.is_symlink():
        raise TaskDocumentError("task authority path may not be a symlink")
    # Lock the durable directory inode: atomic task.md replacement does not
    # invalidate it, and no persistent lock artifact is needed.
    descriptor = os.open(task_file.parent, os.O_RDONLY)
    try:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _read_exact(task_file: Path) -> str:
    with task_file.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def _atomic_replace(task_file: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".task.md.", dir=task_file.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, task_file)
        directory_fd = os.open(task_file.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _canonical_status(value: str) -> str:
    try:
        return _STATUS_ALIASES[value.strip().lower()]
    except KeyError as exc:
        raise TaskDocumentError(f"unsupported task status: {value!r}") from exc


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""
