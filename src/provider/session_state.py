"""Provider-qualified durable Playbook session records.

Task ownership deliberately does not live here. ``task.md`` owns claims;
``current_state`` remains a rebuildable navigation cache beside this record.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from typing import Callable, Iterator, Mapping

from .session_identity import identity_spec, validate_native_session_id


SESSION_RECORD_SCHEMA = 1
_OPTIONAL_FIELDS = frozenset(
    {
        "name",
        "managed",
        "state",
        "project",
        "workspace",
        "resume_cwd",
        "tmux_session",
        "tmux_pane",
        "last_started_at",
        "stopped_at",
        "destroyed_at",
        "body_id",
        "sandbox",
    }
)
_FIELDS = frozenset({"schema", "provider", "session_id", "created_at"}) | _OPTIONAL_FIELDS
_STATES = frozenset({"running", "stopped", "destroyed"})
_MANAGED_ENV_FIELDS = (
    "PLAYBOOK_MANAGED_LAUNCH_TOKEN",
    "PLAYBOOK_MANAGED_BODY_ID",
    "PLAYBOOK_MANAGED_PROJECT_ROOT",
    "PLAYBOOK_PROVIDER",
)
_MANAGED_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_BODY_ID_RE = _MANAGED_NAME_RE
_LAUNCH_SCHEMA = 1
_LAUNCH_OWNER = "playbook-managed-launch/schema-1"


class SessionStateError(ValueError):
    """A session record or its storage boundary is unsafe or contradictory."""


@dataclass(frozen=True)
class SessionKey:
    provider: str
    session_id: str

    @classmethod
    def from_values(cls, provider: str, session_id: object) -> "SessionKey":
        spec = identity_spec(provider)
        return cls(spec.provider, validate_native_session_id(session_id))

    @property
    def directory_name(self) -> str:
        return f"{self.provider}-{self.session_id}"


@dataclass(frozen=True)
class ManagedLaunchReservation:
    token: str
    body_id: str
    path: Path
    provider: str
    name: str | None


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise SessionStateError(f"managed launch {field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SessionStateError(f"managed launch {field} is invalid") from exc
    if parsed.tzinfo is None:
        raise SessionStateError(f"managed launch {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validate_managed_name(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _MANAGED_NAME_RE.fullmatch(value):
        raise SessionStateError(
            "session name must use 1-64 letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return value


def _validate_body_id(value: object) -> str:
    if not isinstance(value, str) or not _BODY_ID_RE.fullmatch(value):
        raise SessionStateError(
            "managed body ID must use 1-64 letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return value


def _sessions_root(agent_dir: Path, *, create: bool) -> Path:
    agent = agent_dir.resolve()
    if agent_dir.is_symlink():
        raise SessionStateError("agent directory may not be a symlink")
    sessions = agent_dir / "sessions"
    if sessions.is_symlink():
        raise SessionStateError("sessions root may not be a symlink")
    if create:
        sessions.mkdir(parents=True, exist_ok=True)
    if not sessions.is_dir():
        raise SessionStateError("sessions root must be a real directory")
    if os.path.commonpath((agent, sessions.resolve())) != str(agent):
        raise SessionStateError("sessions root escapes agent directory")
    return sessions


@contextmanager
def sessions_root_lock(agent_dir: Path, *, create: bool = False) -> Iterator[Path]:
    """Serialize state that spans multiple provider-native session records."""

    sessions = _sessions_root(agent_dir, create=create)
    descriptor = os.open(sessions, os.O_RDONLY)
    try:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if sessions.is_symlink() or not sessions.is_dir():
            raise SessionStateError("sessions root changed while locked")
        yield sessions
    finally:
        os.close(descriptor)


def _atomic_json(
    path: Path,
    value: Mapping[str, object],
    *,
    before_replace: Callable[[], None] | None = None,
) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if before_replace:
            before_replace()
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
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


def _launches_directory(sessions: Path) -> Path:
    launches = sessions / ".launches"
    if launches.is_symlink():
        raise SessionStateError("managed launches directory may not be a symlink")
    launches.mkdir(mode=0o700, exist_ok=True)
    if launches.is_symlink() or not launches.is_dir():
        raise SessionStateError("managed launches directory must be real")
    return launches


def _launch_path(launches: Path, token: str) -> Path:
    if not isinstance(token, str) or len(token) < 16 or "\0" in token:
        raise SessionStateError("managed launch token is malformed")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return launches / f"{digest}.json"


def _validate_launch(value: object, path: Path) -> dict:
    if not isinstance(value, dict):
        raise SessionStateError("managed launch must contain one JSON object")
    required = {
        "schema", "owner", "provider", "name", "body_id", "project", "agent_dir",
        "cwd", "model", "sandbox", "operation", "expected_provider", "expected_session_id",
        "created_at", "expires_at", "state", "claimed_provider", "claimed_session_id",
    }
    if set(value) != required:
        raise SessionStateError(f"managed launch has invalid fields: {path}")
    if value["schema"] != _LAUNCH_SCHEMA or value["owner"] != _LAUNCH_OWNER:
        raise SessionStateError("managed launch ownership/schema is invalid")
    provider = SessionKey.from_values(value["provider"], "validation-id").provider
    if provider != value["provider"]:
        raise SessionStateError("managed launch provider is not canonical")
    _validate_managed_name(value["name"])
    _validate_body_id(value["body_id"])
    for field in ("project", "agent_dir", "cwd"):
        if not isinstance(value[field], str) or not Path(value[field]).is_absolute():
            raise SessionStateError(f"managed launch {field} must be absolute")
    if value["model"] is not None and not isinstance(value["model"], str):
        raise SessionStateError("managed launch model must be a string or null")
    if not isinstance(value["sandbox"], bool):
        raise SessionStateError("managed launch sandbox policy must be boolean")
    if value["operation"] not in {"new", "resume"}:
        raise SessionStateError("managed launch operation is invalid")
    expected = (value["expected_provider"], value["expected_session_id"])
    if value["operation"] == "new" and expected != (None, None):
        raise SessionStateError("new managed launch may not expect an existing session")
    if value["operation"] == "resume":
        SessionKey.from_values(*expected)
    if value["state"] not in {"reserved", "claimed"}:
        raise SessionStateError("managed launch state is invalid")
    claimed = (value["claimed_provider"], value["claimed_session_id"])
    if value["state"] == "reserved" and claimed != (None, None):
        raise SessionStateError("reserved managed launch may not have a claimant")
    if value["state"] == "claimed":
        SessionKey.from_values(*claimed)
    _parse_timestamp(value["created_at"], field="created_at")
    _parse_timestamp(value["expires_at"], field="expires_at")
    return dict(value)


def _remove_expired_launches(launches: Path, *, at: datetime) -> None:
    for path in launches.glob("*.json"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            launch = _validate_launch(json.loads(path.read_text(encoding="utf-8")), path)
        except (OSError, json.JSONDecodeError, SessionStateError):
            continue
        if _parse_timestamp(launch["expires_at"], field="expires_at") < at:
            path.unlink()


def reserve_managed_launch(
    agent_dir: Path,
    *,
    project_root: Path,
    provider: str,
    name: str | None,
    cwd: Path,
    body_id: str | None = None,
    model: str | None = None,
    sandbox: bool = False,
    operation: str = "new",
    expected_key: SessionKey | None = None,
    token: str | None = None,
    now: Callable[[], str] = _timestamp,
    ttl_seconds: float = 600.0,
    before_replace: Callable[[], None] | None = None,
) -> ManagedLaunchReservation:
    """Reserve one human name and a single-use managed-launch capability."""

    key_provider = SessionKey.from_values(provider, "validation-id").provider
    safe_name = _validate_managed_name(name)
    safe_body = _validate_body_id(body_id or f"body-{secrets.token_hex(16)}")
    raw_token = token or secrets.token_urlsafe(32)
    if ttl_seconds <= 0:
        raise SessionStateError("managed launch TTL must be positive")
    if not isinstance(sandbox, bool):
        raise SessionStateError("managed launch sandbox policy must be boolean")
    created_text = now()
    created = _parse_timestamp(created_text, field="created_at")
    expires = (created + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z")
    project = project_root.resolve()
    working = cwd.resolve()
    resolved_agent = agent_dir.resolve()
    if operation not in {"new", "resume"}:
        raise SessionStateError("managed launch operation must be new or resume")
    if operation == "resume" and expected_key is None:
        raise SessionStateError("resume managed launch requires an expected native session")
    if operation == "new" and expected_key is not None:
        raise SessionStateError("new managed launch may not expect a native session")
    if expected_key is not None and expected_key.provider != key_provider:
        raise SessionStateError("resume provider disagrees with expected native session")

    with sessions_root_lock(agent_dir, create=True) as sessions:
        launches = _launches_directory(sessions)
        _remove_expired_launches(launches, at=created)
        launch_path = _launch_path(launches, raw_token)
        if launch_path.exists() or launch_path.is_symlink():
            raise SessionStateError("managed launch token is already reserved")
        if operation == "resume":
            assert expected_key is not None
            expected_directory = session_directory(agent_dir, expected_key)
            expected_record_path = expected_directory / "session.json"
            if expected_record_path.is_symlink() or not expected_record_path.is_file():
                raise SessionStateError("managed resume target record is missing")
            expected_record = _validate_record(
                json.loads(expected_record_path.read_text(encoding="utf-8")),
                expected_key,
            )
            if expected_record.get("managed") is not True or expected_record.get("state") != "stopped":
                raise SessionStateError("managed resume requires a stopped managed session")
            if expected_record.get("name") != safe_name:
                raise SessionStateError("managed resume name changed outside rename")
            assert_no_active_managed_resume(sessions, expected_key, now=now)
        if safe_name is not None:
            for existing_key, directory in iter_session_directories(agent_dir):
                existing = _validate_record(json.loads((directory / "session.json").read_text()), existing_key)
                if existing.get("name") != safe_name or existing.get("state") == "destroyed":
                    continue
                if operation == "resume" and expected_key == existing_key:
                    continue
                raise SessionStateError(f"session name is already in use: {safe_name}")
            for path in launches.glob("*.json"):
                launch = _validate_launch(json.loads(path.read_text(encoding="utf-8")), path)
                if launch["name"] == safe_name:
                    raise SessionStateError(f"session name is already reserved: {safe_name}")
        launch = {
            "schema": _LAUNCH_SCHEMA,
            "owner": _LAUNCH_OWNER,
            "provider": key_provider,
            "name": safe_name,
            "body_id": safe_body,
            "project": str(project),
            "agent_dir": str(resolved_agent),
            "cwd": str(working),
            "model": model,
            "sandbox": sandbox,
            "operation": operation,
            "expected_provider": expected_key.provider if expected_key else None,
            "expected_session_id": expected_key.session_id if expected_key else None,
            "created_at": created_text,
            "expires_at": expires,
            "state": "reserved",
            "claimed_provider": None,
            "claimed_session_id": None,
        }
        _validate_launch(launch, launch_path)
        _atomic_json(launch_path, launch, before_replace=before_replace)
    return ManagedLaunchReservation(raw_token, safe_body, launch_path, key_provider, safe_name)


def assert_no_active_managed_resume(
    sessions: Path,
    key: SessionKey,
    *,
    now: Callable[[], str] = _timestamp,
) -> None:
    """Refuse a lifecycle change while a root-locked resume targets ``key``.

    The caller must hold the sessions-root lock. Expired unclaimed capabilities
    do not retain authority or block later lifecycle operations.
    """

    launches = _launches_directory(sessions)
    current = _parse_timestamp(now(), field="current time")
    _remove_expired_launches(launches, at=current)
    for path in launches.glob("*.json"):
        launch = _validate_launch(
            json.loads(path.read_text(encoding="utf-8")), path
        )
        if (
            launch["operation"] == "resume"
            and launch["expected_provider"] == key.provider
            and launch["expected_session_id"] == key.session_id
        ):
            raise SessionStateError(
                f"managed resume is already in progress for {key.provider}:{key.session_id}"
            )


def cancel_managed_launch(
    agent_dir: Path,
    reservation: ManagedLaunchReservation,
) -> bool:
    """Release only an unclaimed provisional capability and its name.

    A claimed launch may be between native-ID claim and final-record publish;
    deleting it would erase the only recovery witness. Missing means the hook
    already consumed it or another cleanup won.
    """

    with sessions_root_lock(agent_dir, create=True) as sessions:
        launches = _launches_directory(sessions)
        expected = _launch_path(launches, reservation.token)
        if expected != reservation.path:
            raise SessionStateError("managed launch cancellation path disagrees")
        if expected.is_symlink():
            raise SessionStateError("managed launch capability may not be a symlink")
        if not expected.exists():
            return False
        launch = _validate_launch(
            json.loads(expected.read_text(encoding="utf-8")), expected
        )
        if launch["body_id"] != reservation.body_id:
            raise SessionStateError("managed launch cancellation body disagrees")
        if launch["state"] != "reserved":
            return False
        expected.unlink()
        directory_fd = os.open(launches, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True


def _validate_record(value: object, expected: SessionKey | None = None) -> dict:
    if not isinstance(value, dict):
        raise SessionStateError("session.json must contain one JSON object")
    unknown = set(value) - _FIELDS
    if unknown:
        raise SessionStateError(f"session.json contains unknown fields: {sorted(unknown)}")
    if value.get("schema") != SESSION_RECORD_SCHEMA:
        raise SessionStateError("unsupported session.json schema")
    try:
        actual = SessionKey.from_values(value.get("provider"), value.get("session_id"))
    except (KeyError, ValueError) as exc:
        raise SessionStateError(str(exc)) from exc
    if expected is not None and actual != expected:
        raise SessionStateError("session.json identity disagrees with its directory")
    if not isinstance(value.get("created_at"), str) or not value["created_at"]:
        raise SessionStateError("session.json created_at must be a nonempty string")
    for field in ("name", "project", "resume_cwd", "tmux_session", "tmux_pane", "body_id",
                  "last_started_at", "stopped_at", "destroyed_at"):
        if field in value and value[field] is not None and not isinstance(value[field], str):
            raise SessionStateError(f"session.json {field} must be a string or null")
    for field in ("managed", "sandbox"):
        if field in value and not isinstance(value[field], bool):
            raise SessionStateError(f"session.json {field} must be boolean")
    if "state" in value and value["state"] not in _STATES:
        raise SessionStateError("session.json state is invalid")
    if "workspace" in value and not isinstance(value["workspace"], dict):
        raise SessionStateError("session.json workspace must be an object")
    if value.get("body_id") is not None:
        _validate_body_id(value["body_id"])
    return dict(value)


def session_directory(agent_dir: Path, key: SessionKey, *, create: bool = False) -> Path:
    """Return a symlink-free directory confined below ``agent_dir/sessions``."""
    agent = agent_dir.resolve()
    if agent_dir.is_symlink():
        raise SessionStateError("agent directory may not be a symlink")
    sessions = agent_dir / "sessions"
    target = sessions / key.directory_name
    for path, label in ((sessions, "sessions root"), (target, "session entry")):
        if path.is_symlink():
            raise SessionStateError(f"{label} may not be a symlink")
    if sessions.exists() and os.path.commonpath((agent, sessions.resolve())) != str(agent):
        raise SessionStateError("sessions root escapes agent directory")
    if create:
        sessions.mkdir(parents=True, exist_ok=True)
        target.mkdir(exist_ok=True)
    if target.exists() and os.path.commonpath((sessions.resolve(), target.resolve())) != str(sessions.resolve()):
        raise SessionStateError("session entry escapes sessions root")
    return target


def iter_session_directories(agent_dir: Path) -> Iterator[tuple[SessionKey, Path]]:
    """Yield only valid provider-qualified session authorities.

    Everything else under the retained ``sessions/`` parent is inert legacy or
    foreign data. This reader never follows, repairs, reports, or deletes it.
    """
    sessions = agent_dir / "sessions"
    if sessions.is_symlink():
        raise SessionStateError("sessions root may not be a symlink")
    if not sessions.exists():
        return
    for entry in sorted(sessions.iterdir(), key=lambda path: path.name):
        if entry.is_symlink() or not entry.is_dir():
            continue
        record_path = entry / "session.json"
        if record_path.is_symlink() or not record_path.is_file():
            continue
        try:
            record = _validate_record(json.loads(record_path.read_text()))
            key = SessionKey.from_values(record["provider"], record["session_id"])
        except (OSError, json.JSONDecodeError, SessionStateError):
            continue
        if entry.name == key.directory_name:
            yield key, entry


def inspect_session_directories(
    agent_dir: Path,
) -> tuple[tuple[tuple[SessionKey, Path], ...], tuple[Path, ...], tuple[Path, ...]]:
    """Classify native records, inert history, and malformed native candidates."""

    sessions = agent_dir / "sessions"
    if sessions.is_symlink():
        raise SessionStateError("sessions root may not be a symlink")
    if not sessions.exists():
        return (), (), ()
    recognized: list[tuple[SessionKey, Path]] = []
    inert: list[Path] = []
    malformed: list[Path] = []
    for entry in sorted(sessions.iterdir(), key=lambda path: path.name):
        if entry.is_symlink() or not entry.is_dir():
            inert.append(entry)
            continue
        record_path = entry / "session.json"
        if not record_path.exists() and not record_path.is_symlink():
            inert.append(entry)
            continue
        if record_path.is_symlink() or not record_path.is_file():
            malformed.append(record_path)
            continue
        try:
            record = _validate_record(
                json.loads(record_path.read_text(encoding="utf-8"))
            )
            key = SessionKey.from_values(record["provider"], record["session_id"])
        except (OSError, json.JSONDecodeError, SessionStateError):
            malformed.append(record_path)
            continue
        if entry.name != key.directory_name:
            malformed.append(record_path)
            continue
        recognized.append((key, entry))
    return tuple(recognized), tuple(inert), tuple(malformed)


@contextmanager
def _record_lock(directory: Path) -> Iterator[None]:
    # The directory inode is stable while session.json and its auxiliary files
    # are atomically replaced, and locking it creates no state of its own.
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


@contextmanager
def session_state_lock(directory: Path) -> Iterator[None]:
    """Serialize auxiliary state beside one validated native session record."""
    if directory.is_symlink() or not directory.is_dir():
        raise SessionStateError("session state lock requires a real directory")
    record = directory / "session.json"
    if record.is_symlink() or not record.is_file():
        raise SessionStateError("session state lock requires session.json")
    with _record_lock(directory):
        if record.is_symlink() or not record.is_file():
            raise SessionStateError("session state lock requires session.json")
        _validate_record(json.loads(record.read_text(encoding="utf-8")))
        yield


def bind_managed_launch(
    agent_dir: Path,
    provider: str,
    session_id: object,
    *,
    environment: Mapping[str, str],
    now: Callable[[], str] = _timestamp,
) -> tuple[Path, dict] | None:
    """Consume a managed-launch capability for exactly one native session.

    Ordinary provider hooks have no managed markers and return ``None``. The
    managed path validates the project/provider/body tuple before creating even
    a skeleton record, so a resume mismatch cannot become a second session.
    """

    transport_fields = _MANAGED_ENV_FIELDS[:3]
    if not any(environment.get(field) for field in transport_fields):
        return None
    missing = [field for field in _MANAGED_ENV_FIELDS if not environment.get(field)]
    if missing:
        raise SessionStateError(
            f"managed launch environment is incomplete: missing {', '.join(missing)}"
        )
    key = SessionKey.from_values(provider, session_id)
    declared_provider = SessionKey.from_values(
        environment["PLAYBOOK_PROVIDER"], "validation-id"
    ).provider
    if declared_provider != key.provider:
        raise SessionStateError("managed launch provider disagrees with hook provider")
    body_id = _validate_body_id(environment["PLAYBOOK_MANAGED_BODY_ID"])
    project = Path(environment["PLAYBOOK_MANAGED_PROJECT_ROOT"])
    if not project.is_absolute():
        raise SessionStateError("managed launch project root must be absolute")
    project = project.resolve()
    token = environment["PLAYBOOK_MANAGED_LAUNCH_TOKEN"]
    current_text = now()
    current = _parse_timestamp(current_text, field="current time")

    with sessions_root_lock(agent_dir, create=True) as sessions:
        launches = _launches_directory(sessions)
        path = _launch_path(launches, token)
        directory = session_directory(agent_dir, key)
        record_path = directory / "session.json"
        if record_path.is_file() and not record_path.is_symlink():
            existing = _validate_record(
                json.loads(record_path.read_text(encoding="utf-8")), key
            )
            if (
                existing.get("managed") is True
                and existing.get("body_id") == body_id
                and existing.get("project") == str(project)
            ):
                if path.exists() and not path.is_symlink():
                    launch = _validate_launch(
                        json.loads(path.read_text(encoding="utf-8")), path
                    )
                    claimed = (launch["claimed_provider"], launch["claimed_session_id"])
                    if launch["state"] == "claimed" and claimed == (
                        key.provider,
                        key.session_id,
                    ):
                        path.unlink()
                return record_path, existing
        if path.is_symlink():
            raise SessionStateError("managed launch capability may not be a symlink")
        if not path.is_file():
            raise SessionStateError("managed launch capability is unknown or already consumed")
        try:
            launch = _validate_launch(
                json.loads(path.read_text(encoding="utf-8")), path
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionStateError("managed launch capability is malformed") from exc
        if _parse_timestamp(launch["expires_at"], field="expires_at") < current:
            path.unlink()
            raise SessionStateError("managed launch capability expired")
        if launch["provider"] != key.provider:
            raise SessionStateError("managed launch provider binding disagrees")
        if launch["body_id"] != body_id:
            raise SessionStateError("managed launch body binding disagrees")
        if launch["project"] != str(project):
            raise SessionStateError("managed launch project binding disagrees")
        if launch["agent_dir"] != str(agent_dir.resolve()):
            raise SessionStateError("managed launch agent directory binding disagrees")
        claimed = (launch["claimed_provider"], launch["claimed_session_id"])
        if launch["state"] == "claimed" and claimed != (key.provider, key.session_id):
            raise SessionStateError("managed launch was claimed by a different native session")
        expected = (launch["expected_provider"], launch["expected_session_id"])
        if launch["operation"] == "resume" and expected != (key.provider, key.session_id):
            raise SessionStateError(
                "managed resume expected "
                f"{launch['expected_provider']}:{launch['expected_session_id']}, "
                f"received {key.provider}:{key.session_id}"
            )
        if launch["operation"] == "new" and record_path.exists():
            raise SessionStateError("new managed launch collided with an existing native session")
        if launch["operation"] == "resume":
            if not record_path.is_file() or record_path.is_symlink():
                raise SessionStateError("managed resume target record is missing")
            existing = _validate_record(
                json.loads(record_path.read_text(encoding="utf-8")), key
            )
            if existing.get("managed") is not True or existing.get("state") != "stopped":
                raise SessionStateError("managed resume requires a stopped managed session")
            if launch["name"] != existing.get("name"):
                raise SessionStateError("managed resume name changed outside rename")

        if launch["state"] == "reserved":
            launch.update(
                state="claimed",
                claimed_provider=key.provider,
                claimed_session_id=key.session_id,
            )
            _atomic_json(path, launch)
        enrich: dict[str, object] = {
            "managed": True,
            "state": "running",
            "project": str(project),
            "workspace": {"mode": "current", "path": str(project)},
            "resume_cwd": launch["cwd"],
            "body_id": body_id,
            "sandbox": launch["sandbox"],
            "last_started_at": current_text,
            "stopped_at": None,
            "destroyed_at": None,
        }
        if launch["name"] is not None:
            enrich["name"] = launch["name"]
        record_path, record = ensure_session_record(
            agent_dir,
            key.provider,
            key.session_id,
            enrich=enrich,
            now=lambda: current_text,
            environment={},
        )
        path.unlink()
        launches_fd = os.open(launches, os.O_RDONLY)
        try:
            os.fsync(launches_fd)
        finally:
            os.close(launches_fd)
        return record_path, record


def ensure_session_record(
    agent_dir: Path,
    provider: str,
    session_id: object,
    *,
    enrich: Mapping[str, object] | None = None,
    expected: Mapping[str, object] | None = None,
    now: Callable[[], str] = _timestamp,
    before_replace: Callable[[], None] | None = None,
    environment: Mapping[str, str] | None = None,
) -> tuple[Path, dict]:
    """Atomically create or enrich one identity-bound ``session.json``."""
    key = SessionKey.from_values(provider, session_id)
    managed = bind_managed_launch(
        agent_dir,
        key.provider,
        key.session_id,
        environment=os.environ if environment is None else environment,
        now=now,
    )
    if managed is not None:
        if not enrich:
            return managed
        # Bootstrap may add descriptive cwd/project fields after the first hook
        # consumed the launch. Re-enter without the inherited capability.
        return ensure_session_record(
            agent_dir,
            key.provider,
            key.session_id,
            enrich=enrich,
            expected=expected,
            now=now,
            before_replace=before_replace,
            environment={},
        )
    updates = dict(enrich or {})
    forbidden = set(updates) - _OPTIONAL_FIELDS
    if forbidden:
        raise SessionStateError(f"cannot enrich reserved/unknown fields: {sorted(forbidden)}")
    comparisons = dict(expected or {})
    unknown_expected = set(comparisons) - _FIELDS
    if unknown_expected:
        raise SessionStateError(
            f"cannot compare unknown session fields: {sorted(unknown_expected)}"
        )
    # Validate the complete update shape before creating any path.
    probe = {
        "schema": SESSION_RECORD_SCHEMA,
        "provider": key.provider,
        "session_id": key.session_id,
        "created_at": "validation-probe",
        **updates,
    }
    _validate_record(probe, key)
    try:
        json.dumps(probe)
    except (TypeError, ValueError) as exc:
        raise SessionStateError(f"session.json values must be JSON-compatible: {exc}") from exc
    directory = session_directory(agent_dir, key, create=True)
    record_path = directory / "session.json"
    with _record_lock(directory):
        if record_path.is_symlink():
            raise SessionStateError("session.json may not be a symlink")
        if record_path.exists():
            try:
                current = _validate_record(json.loads(record_path.read_text()), key)
            except (OSError, json.JSONDecodeError) as exc:
                raise SessionStateError(f"invalid session.json: {exc}") from exc
        else:
            planted = sorted(
                entry.name for entry in directory.iterdir()
                if entry.name != ".session.lock"
                and not entry.name.startswith(".session.json.")
            )
            if planted:
                raise SessionStateError(
                    "refusing to adopt nonempty session directory without "
                    f"session.json: {planted}"
                )
            current = {
                "schema": SESSION_RECORD_SCHEMA,
                "provider": key.provider,
                "session_id": key.session_id,
                "created_at": now(),
            }
        changed = [
            field
            for field, expected_value in comparisons.items()
            if current.get(field) != expected_value
        ]
        if changed:
            raise SessionStateError(
                f"session record changed before update: {', '.join(sorted(changed))}"
            )
        candidate = _validate_record({**current, **updates}, key)
        if candidate == current and record_path.exists():
            return record_path, current
        descriptor, temporary = tempfile.mkstemp(prefix=".session.json.", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(candidate, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if before_replace:
                before_replace()
            os.replace(temporary, record_path)
            directory_fd = os.open(directory, os.O_RDONLY)
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
        return record_path, candidate


def write_navigation_cache(
    agent_dir: Path,
    key: SessionKey,
    task_number: str,
    *,
    before_replace: Callable[[], None] | None = None,
) -> Path:
    """Atomically publish the rebuildable session→task navigation cache."""
    if not task_number or not str(task_number).isdigit():
        raise SessionStateError("navigation cache task number must be numeric")
    directory = session_directory(agent_dir, key)
    record_path = directory / "session.json"
    if not record_path.is_file() or record_path.is_symlink():
        raise SessionStateError("navigation cache requires a valid session.json")
    _validate_record(json.loads(record_path.read_text(encoding="utf-8")), key)
    state = directory / "current_state"
    with _record_lock(directory):
        descriptor, temporary = tempfile.mkstemp(prefix=".current_state.", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(f"{int(task_number):03d}\n")
                stream.flush()
                os.fsync(stream.fileno())
            if before_replace:
                before_replace()
            os.replace(temporary, state)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
    return state


def clear_navigation_cache(
    agent_dir: Path,
    key: SessionKey,
    *,
    expected_task: str | None = None,
) -> bool:
    """Clear only the expected rebuildable pointer; preserve the session record."""
    directory = session_directory(agent_dir, key)
    record_path = directory / "session.json"
    if not record_path.is_file() or record_path.is_symlink():
        raise SessionStateError("navigation cache requires a valid session.json")
    _validate_record(json.loads(record_path.read_text(encoding="utf-8")), key)
    state = directory / "current_state"
    with _record_lock(directory):
        if not state.exists():
            return False
        if expected_task is not None:
            current = state.read_text(encoding="utf-8").strip()
            try:
                matches = int(current) == int(expected_task)
            except ValueError as exc:
                raise SessionStateError(
                    f"navigation cache is malformed: {state}"
                ) from exc
            if not matches:
                return False
        state.unlink()
        return True
