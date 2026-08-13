"""Serialized project chat-log state shared by provider hooks and maintenance."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Iterator
import uuid

from provider.session_state import SessionKey, session_directory


CHAT_HEADER = (
    "# Project Chat Log\n\n"
    "User messages and sparse Playbook events logged with timestamps.\n\n"
)

_MARKER_RE = re.compile(
    r"^(?:M\d+|G\d+:\d+|T\d+:[a-z][a-z0-9-]*|S:[a-z][a-z0-9-]*)$"
)
_EVENT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ENTRY_HEADER_RE = re.compile(
    r"^\*\*\[(?P<marker>"
    r"M\d+|G\d+:\d+|T\d+:[a-z][a-z0-9-]*|S:[a-z][a-z0-9-]*"
    r")\]\*\*\s+\[(?P<timestamp>[^\]\r\n]+)\]\s+"
    r"`(?P<speaker>[A-Za-z0-9_-]+)`(?P<suffix>[^\r\n]*)$",
    re.MULTILINE,
)
_BODY_HEADER_LINE_RE = re.compile(
    r"^(?=\*\*\[(?:"
    r"M\d+|G\d+:\d+|T\d+:[a-z][a-z0-9-]*|S:[a-z][a-z0-9-]*|"
    r"[^\]\r\n]* UTC"
    r")\]\*\*)",
    re.MULTILINE,
)


def _encode_body(message: str) -> str:
    """Keep body lines readable while preventing them from becoming entries."""
    return _BODY_HEADER_LINE_RE.sub(lambda _match: "\\", message)


def _decode_body(message: str) -> str:
    return re.sub(
        rf"^\\(?={_BODY_HEADER_LINE_RE.pattern[1:]})",
        "",
        message,
        flags=re.MULTILINE,
    )


@dataclass(frozen=True)
class ChatEntry:
    """One parsed chat-log entry; historical rows may lack session metadata."""

    marker: str
    timestamp: str
    speaker: str
    body: str
    provider: str | None = None
    session_id: str | None = None
    session_name: str | None = None
    event_key: str | None = None
    raw: str = ""


@contextmanager
def chat_log_lock(agent_dir: Path) -> Iterator[None]:
    """Hold the one advisory lock honored by every shipped chat mutator."""
    if agent_dir.is_symlink() or not agent_dir.is_dir():
        raise OSError("chat lock requires a real agent directory")
    # Lock the durable agent-directory inode: no persistent lock artifact is
    # created, and atomic chat-log replacement cannot invalidate the lock.
    descriptor = os.open(agent_dir, os.O_RDONLY)
    try:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _migrate_old_entries(text: str) -> tuple[str, int]:
    if re.search(r"^\*\*\[M\d+\]\*\*", text, re.MULTILINE):
        mids = [int(value) for value in re.findall(r"\*\*\[M(\d+)\]\*\*", text)]
        return text, max(mids, default=0)
    number = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal number
        number += 1
        return f"**[M{number:03d}]** [{match.group(1)}]"

    migrated = re.sub(
        r"^\*\*\[([^\]]* UTC)\]\*\*",
        replace,
        text,
        flags=re.MULTILINE,
    )
    return migrated, number


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _metadata_from_suffix(
    suffix: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Parse the identity-bearing parenthesis group, ignoring legacy notes."""
    for raw_group in re.findall(r"\(([^()]*)\)", suffix):
        parts = [part.strip() for part in raw_group.split(";")]
        provider, separator, session_id = parts[0].partition("/")
        if not separator:
            continue
        try:
            key = SessionKey.from_values(provider, session_id)
        except (KeyError, TypeError, ValueError):
            continue
        values: dict[str, str] = {}
        for part in parts[1:]:
            field, field_separator, value = part.partition("=")
            if field_separator:
                values[field.strip()] = value.strip()
        name = values.get("name")
        if name == "-":
            name = None
        return key.provider, key.session_id, name, values.get("event")
    return None, None, None, None


def parse_chat_entries(text: str) -> list[ChatEntry]:
    """Parse current and historical M/G/T/S blocks without rewriting them."""
    entries: list[ChatEntry] = []
    matches = list(_ENTRY_HEADER_RE.finditer(text))
    for index, match in enumerate(matches):
        provider, session_id, session_name, event_key = _metadata_from_suffix(
            match.group("suffix")
        )
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        raw = text[match.start():end].strip()
        raw = re.sub(r"(?:^|\n)---\s*$", "", raw).strip()
        body = text[match.end():end].strip()
        body = re.sub(r"(?:^|\n)---\s*$", "", body).strip()
        body = _decode_body(body)
        entries.append(
            ChatEntry(
                marker=match.group("marker"),
                timestamp=match.group("timestamp"),
                speaker=match.group("speaker"),
                body=body,
                provider=provider,
                session_id=session_id,
                session_name=session_name,
                event_key=event_key,
                raw=raw,
            )
        )
    return entries


def _default_event_key(
    marker: str,
    key: SessionKey,
    timestamp: str,
    message: str,
) -> str:
    marker_kind = "M" if marker.startswith("M") else marker
    if marker_kind == "M":
        # Equal human prompts are still distinct invocations. Producers that
        # can replay one invocation must provide their own stable event key.
        return uuid.uuid4().hex[:24]
    digest = hashlib.sha256(
        "\0".join(
            (marker_kind, key.provider, key.session_id, timestamp, message)
        ).encode("utf-8")
    ).hexdigest()
    return digest[:24]


def derive_event_key(*parts: object) -> str:
    """Return a stable bounded key for one producer-owned transition."""
    digest = hashlib.sha256(
        "\0".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()
    return digest[:24]


def chat_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _recorded_session_name(agent_dir: Path, key: SessionKey) -> str | None:
    """Read an optional event-time label; identity never depends on this hint."""
    try:
        directory = session_directory(agent_dir, key)
        record_path = directory / "session.json"
        if record_path.is_symlink() or not record_path.is_file():
            return None
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("provider") != key.provider or record.get("session_id") != key.session_id:
        return None
    value = record.get("name")
    if isinstance(value, str) and _SESSION_NAME_RE.fullmatch(value):
        return value
    return None


def _validate_entry_values(
    marker: str,
    timestamp: str,
    message: str,
    session_name: str | None,
    event_key: str,
) -> None:
    if not _MARKER_RE.fullmatch(marker):
        raise ValueError(f"invalid chat event marker: {marker}")
    if not timestamp or "\n" in timestamp or "\r" in timestamp:
        raise ValueError("chat timestamp must be one line")
    if not message:
        raise ValueError("chat message must be nonempty")
    if session_name is not None and not _SESSION_NAME_RE.fullmatch(session_name):
        raise ValueError("invalid chat session name")
    if not _EVENT_KEY_RE.fullmatch(event_key):
        raise ValueError("invalid chat event key")


def _append_entry(
    agent_dir: Path,
    marker: str,
    provider: str,
    session_id: str,
    timestamp: str,
    message: str,
    *,
    session_name: str | None,
    event_key: str | None,
) -> tuple[str, int | None]:
    key = SessionKey.from_values(provider, session_id)
    if session_name is None:
        session_name = _recorded_session_name(agent_dir, key)
    scoped_event_key = event_key or _default_event_key(
        marker, key, timestamp, message
    )
    _validate_entry_values(
        marker, timestamp, message, session_name, scoped_event_key
    )
    log = agent_dir / "chat_log.md"
    counter = agent_dir / "chat_log_counter"
    agent_dir.mkdir(parents=True, exist_ok=True)
    with chat_log_lock(agent_dir):
        text = log.read_text(encoding="utf-8") if log.exists() else CHAT_HEADER
        text, migrated_highest = _migrate_old_entries(text)
        counter_value = 0
        if counter.exists():
            try:
                counter_value = int(counter.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                counter_value = 0
        highest = max(
            migrated_highest,
            counter_value,
            max(
                (int(value) for value in re.findall(r"\*\*\[M(\d+)\]\*\*", text)),
                default=0,
            ),
        )
        rendered_marker = f"M{highest + 1:03d}" if marker == "M0" else marker
        scope_marker = "M" if rendered_marker.startswith("M") else rendered_marker
        for entry in parse_chat_entries(text):
            entry_scope = "M" if entry.marker.startswith("M") else entry.marker
            if (
                entry_scope,
                entry.provider,
                entry.session_id,
                entry.event_key,
            ) != (scope_marker, key.provider, key.session_id, scoped_event_key):
                continue
            if (
                entry.body != message
                or entry.session_name != session_name
            ):
                raise ValueError("chat event key already identifies different content")
            mid = int(entry.marker[1:]) if entry.marker.startswith("M") else None
            return entry.marker, mid

        name = session_name or "-"
        metadata = (
            f"{key.provider}/{key.session_id}; name={name}; event={scoped_event_key}"
        )
        tag = f"**[{rendered_marker}]** [{timestamp}] `HOST` ({metadata})"
        if not text.endswith("\n"):
            text += "\n"
        text += f"---\n\n{tag}\n\n{_encode_body(message)}\n\n"
        _atomic_write(log, text)
        mid = None
        if rendered_marker.startswith("M"):
            mid = int(rendered_marker[1:])
            _atomic_write(counter, f"{mid}\n")
        return rendered_marker, mid


def append_chat_event(
    agent_dir: Path,
    marker: str,
    provider: str,
    session_id: str,
    timestamp: str,
    message: str,
    *,
    session_name: str | None = None,
    event_key: str | None = None,
) -> str:
    """Append one attributed non-message event and return its stable marker."""
    if marker.startswith("M"):
        raise ValueError("message markers are allocated by append_chat_message")
    rendered, _mid = _append_entry(
        agent_dir,
        marker,
        provider,
        session_id,
        timestamp,
        message,
        session_name=session_name,
        event_key=event_key,
    )
    return rendered


def append_chat_message(
    agent_dir: Path,
    provider: str,
    session_id: str,
    timestamp: str,
    message: str,
    *,
    session_name: str | None = None,
    event_key: str | None = None,
) -> int:
    """Append one complete attributed message and return its monotonic MID."""
    _marker, mid = _append_entry(
        agent_dir,
        "M0",
        provider,
        session_id,
        timestamp,
        message,
        session_name=session_name,
        event_key=event_key,
    )
    assert mid is not None
    return mid
