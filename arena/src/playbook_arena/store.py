"""Exclusive, append-only evidence storage for arena runs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from .case import ArenaCaseError, safe_relative
from .schema import canonical_json


EVENT_SCHEMA = 1
ZERO_HASH = "0" * 64
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _real_dir(path: Path, *, create: bool = False) -> Path:
    if create:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ArenaCaseError(f"arena directory must be a real directory: {path}")
    return path.resolve()


def _open_no_follow(path: Path, flags: int, mode: int = 0o600) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags | nofollow, mode)
    except OSError as exc:
        raise ArenaCaseError(f"cannot open arena evidence {path}: {exc}") from exc


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise ArenaCaseError("short write while recording arena evidence")
        offset += written


def _event_hash(record: dict[str, Any]) -> str:
    payload = dict(record)
    payload.pop("hash", None)
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _artifact_target(root: Path, relative: str, *, create_parents: bool) -> Path:
    current = root
    parts = Path(relative).parts
    for part in parts[:-1]:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise ArenaCaseError(f"artifact parent must be a real directory: {relative}")
        elif create_parents:
            current.mkdir(mode=0o700)
        else:
            raise ArenaCaseError(f"artifact parent is missing: {relative}")
    return current / parts[-1]


@dataclass(frozen=True)
class VerifiedEvent:
    sequence: int
    type: str
    payload: dict[str, Any]
    hash: str


class RunStore:
    def __init__(self, run_root: Path, lock: BinaryIO, *, read_only: bool = False) -> None:
        self.root = run_root
        self.events_path = run_root / "events.jsonl"
        self.state_path = run_root / "state.json"
        self.artifacts = run_root / "artifacts"
        self._lock = lock
        self._read_only = read_only

    @classmethod
    def reserve(cls, results_root: str | Path, run_id: str, identity: dict[str, Any]) -> "RunStore":
        safe = safe_relative(run_id, label="run id")
        if "/" in safe:
            raise ArenaCaseError("run id must be one safe path component")
        root = _real_dir(Path(results_root), create=True)
        run_root = root / safe
        try:
            run_root.mkdir(mode=0o700)
            (run_root / "artifacts").mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ArenaCaseError(f"run already exists: {run_id}") from exc
        lock_fd = _open_no_follow(run_root / "owner.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL)
        lock = os.fdopen(lock_fd, "r+b", buffering=0)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = cls(run_root.resolve(), lock)
        store.append("run_reserved", identity, state="reserved")
        return store

    @classmethod
    def read_only(cls, run_root: str | Path) -> "RunStore":
        root = _real_dir(Path(run_root))
        lock_fd = _open_no_follow(root / "owner.lock", os.O_RDONLY)
        lock = os.fdopen(lock_fd, "rb", buffering=0)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock.close()
            raise ArenaCaseError(f"run still has a live writer: {root.name}") from exc
        store = cls(root, lock, read_only=True)
        store.verify()
        store.verify_artifacts()
        return store

    @classmethod
    def resume(cls, run_root: str | Path) -> "RunStore":
        root = _real_dir(Path(run_root))
        lock_fd = _open_no_follow(root / "owner.lock", os.O_RDWR)
        lock = os.fdopen(lock_fd, "r+b", buffering=0)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock.close()
            raise ArenaCaseError(f"run already has a live owner: {root.name}") from exc
        store = cls(root, lock)
        events = store.verify()
        snapshot = store.read_state()
        if snapshot["last_hash"] != events[-1].hash or snapshot["sequence"] != events[-1].sequence:
            store.close()
            raise ArenaCaseError("run state snapshot conflicts with event chain")
        store.append("run_resumed", {"previous_state": snapshot["state"]}, state=snapshot["state"])
        return store

    def close(self) -> None:
        if not self._lock.closed:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            self._lock.close()

    def __enter__(self) -> "RunStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def verify(self) -> list[VerifiedEvent]:
        if self.events_path.is_symlink() or not self.events_path.is_file():
            raise ArenaCaseError("event log must be a real file")
        events: list[VerifiedEvent] = []
        previous = ZERO_HASH
        try:
            lines = self.events_path.read_bytes().splitlines()
        except OSError as exc:
            raise ArenaCaseError(f"cannot read event log: {exc}") from exc
        if not lines:
            raise ArenaCaseError("event log is empty")
        for expected, line in enumerate(lines, start=1):
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ArenaCaseError(f"event {expected} is invalid JSON: {exc}") from exc
            if not isinstance(record, dict) or set(record) != {"schema", "sequence", "time", "type", "payload", "previous_hash", "hash"}:
                raise ArenaCaseError(f"event {expected} fields are invalid")
            if record["schema"] != EVENT_SCHEMA or record["sequence"] != expected or record["previous_hash"] != previous:
                raise ArenaCaseError(f"event {expected} sequence/hash chain is invalid")
            if not isinstance(record["type"], str) or not record["type"] or not isinstance(record["payload"], dict):
                raise ArenaCaseError(f"event {expected} content is invalid")
            digest = _event_hash(record)
            if record["hash"] != digest:
                raise ArenaCaseError(f"event {expected} hash mismatch")
            events.append(VerifiedEvent(expected, record["type"], record["payload"], digest))
            previous = digest
        return events

    def append(self, event_type: str, payload: dict[str, Any], *, state: str) -> VerifiedEvent:
        if self._read_only:
            raise ArenaCaseError("read-only run store cannot append")
        if not isinstance(event_type, str) or not event_type or not isinstance(payload, dict):
            raise ArenaCaseError("event type/payload is invalid")
        if self.events_path.exists():
            events = self.verify()
            sequence = events[-1].sequence + 1
            previous = events[-1].hash
            flags = os.O_WRONLY | os.O_APPEND
        else:
            sequence = 1
            previous = ZERO_HASH
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        record: dict[str, Any] = {
            "schema": EVENT_SCHEMA,
            "sequence": sequence,
            "time": utc_timestamp(),
            "type": event_type,
            "payload": payload,
            "previous_hash": previous,
        }
        record["hash"] = _event_hash(record)
        encoded = canonical_json(record) + b"\n"
        fd = _open_no_follow(self.events_path, flags)
        try:
            _write_all(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        self._write_state(sequence, record["hash"], state)
        return VerifiedEvent(sequence, event_type, payload, record["hash"])

    def _write_state(self, sequence: int, last_hash: str, state: str) -> None:
        value = {"schema": EVENT_SCHEMA, "sequence": sequence, "last_hash": last_hash, "state": state}
        fd, temporary = tempfile.mkstemp(prefix=".state-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(canonical_json(value) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def read_state(self) -> dict[str, Any]:
        if self.state_path.is_symlink() or not self.state_path.is_file():
            raise ArenaCaseError("run state must be a real file")
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArenaCaseError(f"cannot read run state: {exc}") from exc
        if not isinstance(value, dict) or set(value) != {"schema", "sequence", "last_hash", "state"}:
            raise ArenaCaseError("run state fields are invalid")
        return value

    def record_artifact(self, source: str | Path, relative: str, *, kind: str, state: str) -> dict[str, Any]:
        if self._read_only:
            raise ArenaCaseError("read-only run store cannot record artifacts")
        safe = safe_relative(relative, label="artifact path")
        target = _artifact_target(self.artifacts, safe, create_parents=True)
        source_path = Path(source)
        if source_path.is_symlink() or not source_path.is_file():
            raise ArenaCaseError(f"artifact source must be a real file: {source_path}")
        source_stat = source_path.stat()
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size > MAX_ARTIFACT_BYTES:
            raise ArenaCaseError(f"artifact source is invalid or too large: {source_path}")
        if target.exists() or target.is_symlink():
            raise ArenaCaseError(f"artifact already exists: {safe}")
        data = source_path.read_bytes()
        fd = _open_no_follow(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        digest = hashlib.sha256(data).hexdigest()
        payload = {"kind": kind, "path": safe, "bytes": len(data), "sha256": digest}
        self.append("artifact_recorded", payload, state=state)
        return payload

    def verify_artifacts(self) -> list[dict[str, Any]]:
        expected = [event.payload for event in self.verify() if event.type == "artifact_recorded"]
        seen = set()
        for item in expected:
            safe = safe_relative(item.get("path"), label="artifact path")
            if safe in seen:
                raise ArenaCaseError(f"duplicate artifact event: {safe}")
            seen.add(safe)
            path = _artifact_target(self.artifacts, safe, create_parents=False)
            if path.is_symlink() or not path.is_file():
                raise ArenaCaseError(f"artifact is missing or not regular: {safe}")
            data = path.read_bytes()
            if len(data) != item.get("bytes") or hashlib.sha256(data).hexdigest() != item.get("sha256"):
                raise ArenaCaseError(f"artifact integrity mismatch: {safe}")
        actual = {
            path.relative_to(self.artifacts).as_posix()
            for path in self.artifacts.rglob("*")
            if path.is_file() or path.is_symlink()
        }
        if actual != seen:
            raise ArenaCaseError("artifact directory contains unrecorded files")
        return expected
