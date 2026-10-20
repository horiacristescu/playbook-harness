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
_LANE_SCHEMA = 1
_LANE_OWNER = "playbook-managed-lane/schema-1"
_LANE_STATES = frozenset(
    {
        "provisional",
        "shell_idle",
        "launch_reserved",
        "provider_running",
        "provider_exited",
        "relaunching",
        "destroyed",
    }
)


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


@dataclass(frozen=True)
class ManagedLane:
    """A durable tmux lane whose identity outlives provider generations."""

    path: Path
    record: dict


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


def _lanes_directory(sessions: Path) -> Path:
    lanes = sessions / ".lanes"
    if lanes.is_symlink():
        raise SessionStateError("managed lanes directory may not be a symlink")
    lanes.mkdir(mode=0o700, exist_ok=True)
    if lanes.is_symlink() or not lanes.is_dir():
        raise SessionStateError("managed lanes directory must be real")
    return lanes


def _lane_path(lanes: Path, body_id: str) -> Path:
    return lanes / f"{_validate_body_id(body_id)}.json"


def _validate_lane(value: object, path: Path) -> dict:
    if not isinstance(value, dict):
        raise SessionStateError("managed lane must contain one JSON object")
    required = {
        "schema", "owner", "name", "body_id", "project", "cwd", "state",
        "generation", "launch_provider", "foreground_process_group", "provider",
        "session_id", "last_bound_at", "created_at", "updated_at",
    }
    if set(value) != required:
        raise SessionStateError(f"managed lane has invalid fields: {path}")
    if value["schema"] != _LANE_SCHEMA or value["owner"] != _LANE_OWNER:
        raise SessionStateError("managed lane ownership/schema is invalid")
    _validate_managed_name(value["name"])
    _validate_body_id(value["body_id"])
    for field in ("project", "cwd"):
        if not isinstance(value[field], str) or not Path(value[field]).is_absolute():
            raise SessionStateError(f"managed lane {field} must be absolute")
    if value["state"] not in _LANE_STATES:
        raise SessionStateError("managed lane state is invalid")
    if not isinstance(value["generation"], int) or value["generation"] < 0:
        raise SessionStateError("managed lane generation must be a nonnegative integer")
    launch_provider = value["launch_provider"]
    if launch_provider is not None:
        canonical = SessionKey.from_values(launch_provider, "validation-id").provider
        if launch_provider != canonical:
            raise SessionStateError("managed lane launch provider is not canonical")
    foreground = value["foreground_process_group"]
    if foreground is not None and (not isinstance(foreground, int) or foreground <= 1):
        raise SessionStateError("managed lane foreground process group is invalid")
    provider, session_id = value["provider"], value["session_id"]
    if (provider is None) != (session_id is None):
        raise SessionStateError("managed lane provider identity must be complete or absent")
    if provider is not None:
        SessionKey.from_values(provider, session_id)
    _parse_timestamp(value["created_at"], field="created_at")
    _parse_timestamp(value["updated_at"], field="updated_at")
    if value["last_bound_at"] is not None:
        _parse_timestamp(value["last_bound_at"], field="last_bound_at")
    return dict(value)


def create_managed_lane(
    agent_dir: Path,
    *,
    project_root: Path,
    name: str | None,
    cwd: Path,
    body_id: str | None = None,
    now: Callable[[], str] = _timestamp,
) -> ManagedLane:
    """Create the name-addressable authority for one durable shell lane."""

    safe_name = _validate_managed_name(name)
    safe_body = _validate_body_id(body_id or f"body-{secrets.token_hex(16)}")
    timestamp = now()
    _parse_timestamp(timestamp, field="created_at")
    with sessions_root_lock(agent_dir, create=True) as sessions:
        lanes = _lanes_directory(sessions)
        path = _lane_path(lanes, safe_body)
        if path.exists() or path.is_symlink():
            raise SessionStateError(f"managed lane body is already in use: {safe_body}")
        for candidate in lanes.glob("*.json"):
            lane = _validate_lane(json.loads(candidate.read_text(encoding="utf-8")), candidate)
            if lane["state"] != "destroyed" and safe_name is not None and lane["name"] == safe_name:
                raise SessionStateError(f"session name is already in use: {safe_name}")
        record = {
            "schema": _LANE_SCHEMA,
            "owner": _LANE_OWNER,
            "name": safe_name,
            "body_id": safe_body,
            "project": str(project_root.resolve()),
            "cwd": str(cwd.resolve()),
            "state": "provisional",
            "generation": 0,
            "launch_provider": None,
            "foreground_process_group": None,
            "provider": None,
            "session_id": None,
            "last_bound_at": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        _validate_lane(record, path)
        _atomic_json(path, record)
        return ManagedLane(path, record)


def resolve_managed_lane(
    agent_dir: Path, address: str, *, include_destroyed: bool = False
) -> ManagedLane:
    """Resolve a lane by human name or body ID, independently of provider binding.

    A live lane always wins. With ``include_destroyed`` and no live match, the
    most recently updated destroyed lane answers, so a lane whose tmux body
    died in a reboot can still say which conversation it last ran.
    """

    if not address:
        raise SessionStateError("lane address may not be empty")
    with sessions_root_lock(agent_dir, create=True) as sessions:
        lanes = _lanes_directory(sessions)
        matches: list[ManagedLane] = []
        destroyed: list[ManagedLane] = []
        for path in lanes.glob("*.json"):
            if path.is_symlink() or not path.is_file():
                continue
            lane = _validate_lane(json.loads(path.read_text(encoding="utf-8")), path)
            if lane["body_id"] != address and lane["name"] != address:
                continue
            if lane["state"] == "destroyed":
                destroyed.append(ManagedLane(path, lane))
            else:
                matches.append(ManagedLane(path, lane))
        if not matches and include_destroyed and destroyed:
            return max(destroyed, key=lambda lane: lane.record["updated_at"])
        if not matches:
            raise SessionStateError(f"lane not found: {address}")
        if len(matches) != 1:
            raise SessionStateError(f"lane address is ambiguous: {address}")
        return matches[0]


def list_managed_lanes(
    agent_dir: Path, *, include_destroyed: bool = False
) -> list[ManagedLane]:
    """Every lane record, oldest first."""

    if not (agent_dir / "sessions").is_dir():
        return []
    with sessions_root_lock(agent_dir, create=False) as sessions:
        lanes = _lanes_directory(sessions)
        found = []
        for path in lanes.glob("*.json"):
            if path.is_symlink() or not path.is_file():
                continue
            lane = _validate_lane(json.loads(path.read_text(encoding="utf-8")), path)
            if include_destroyed or lane["state"] != "destroyed":
                found.append(ManagedLane(path, lane))
    return sorted(found, key=lambda lane: lane.record["created_at"])


def lane_name_for_body(agent_dir: Path, body_id: object) -> str | None:
    """Read a lane's human name for a label; never used as identity."""

    if not isinstance(body_id, str):
        return None
    try:
        path = _lane_path(agent_dir / "sessions" / ".lanes", body_id)
        if path.is_symlink() or not path.is_file():
            return None
        name = json.loads(path.read_text(encoding="utf-8")).get("name")
    except (OSError, ValueError, SessionStateError, AttributeError):
        return None
    return name if isinstance(name, str) and _MANAGED_NAME_RE.fullmatch(name) else None


class LaneTransitionConflict(SessionStateError):
    """Another controller moved the lane first; re-read it and decide again."""


def transition_managed_lane(
    agent_dir: Path,
    body_id: str,
    *,
    expected_state: str,
    state: str,
    expected_generation: int | None = None,
    provider: str | None = None,
    session_id: str | None = None,
    launch_provider: str | None = None,
    foreground_process_group: int | None = None,
    last_bound_at: str | None = None,
    advance_generation: bool = False,
    now: Callable[[], str] = _timestamp,
) -> ManagedLane:
    """Compare-and-swap one lane transition under the sessions-root lock."""

    if expected_state not in _LANE_STATES or state not in _LANE_STATES:
        raise SessionStateError("managed lane transition state is invalid")
    timestamp = now()
    _parse_timestamp(timestamp, field="updated_at")
    with sessions_root_lock(agent_dir, create=False) as sessions:
        path = _lane_path(_lanes_directory(sessions), body_id)
        if path.is_symlink() or not path.is_file():
            raise SessionStateError(f"lane not found: {body_id}")
        current = _validate_lane(json.loads(path.read_text(encoding="utf-8")), path)
        if current["state"] != expected_state:
            raise LaneTransitionConflict(
                f"managed lane {body_id} is {current['state']}, expected {expected_state}"
            )
        if expected_generation is not None and current["generation"] != expected_generation:
            raise LaneTransitionConflict(
                f"managed lane {body_id} is at generation {current['generation']}, "
                f"expected {expected_generation}"
            )
        if (provider is None) != (session_id is None):
            raise SessionStateError("managed lane provider identity must be complete or absent")
        if provider is not None:
            key = SessionKey.from_values(provider, session_id)
            provider, session_id = key.provider, key.session_id
        if launch_provider is not None:
            launch_provider = SessionKey.from_values(
                launch_provider, "validation-id"
            ).provider
        if foreground_process_group is not None and foreground_process_group <= 1:
            raise SessionStateError("managed lane foreground process group is invalid")
        updated = dict(current)
        updated.update(
            {
                "state": state,
                "provider": provider,
                "session_id": session_id,
                "launch_provider": launch_provider,
                "foreground_process_group": foreground_process_group,
                "last_bound_at": last_bound_at,
                "updated_at": timestamp,
            }
        )
        if advance_generation:
            updated["generation"] += 1
        _validate_lane(updated, path)
        _atomic_json(path, updated)
        return ManagedLane(path, updated)


def _finalize_lane_binding_locked(
    sessions: Path,
    *,
    body_id: str,
    key: SessionKey,
    bound_at: str,
) -> None:
    """Publish hook-observed native identity into an admitted lane generation."""

    lanes = _lanes_directory(sessions)
    path = _lane_path(lanes, body_id)
    if not path.exists():
        return  # Compatibility for pre-lane managed sessions.
    if path.is_symlink() or not path.is_file():
        raise SessionStateError("managed lane authority is unsafe")
    lane = _validate_lane(json.loads(path.read_text(encoding="utf-8")), path)
    if lane["state"] not in {"launch_reserved", "relaunching", "provider_running"}:
        raise SessionStateError(
            f"managed lane cannot bind from state {lane['state']}: {body_id}"
        )
    if lane["launch_provider"] != key.provider:
        raise SessionStateError("managed lane admitted a different provider")
    existing = (lane["provider"], lane["session_id"])
    if existing != (None, None) and existing != (key.provider, key.session_id):
        raise SessionStateError("managed lane generation is already bound differently")
    lane.update(
        state="provider_running",
        provider=key.provider,
        session_id=key.session_id,
        last_bound_at=bound_at,
        updated_at=bound_at,
    )
    _validate_lane(lane, path)
    _atomic_json(path, lane)


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
        if body_id is not None:
            # A body launches one generation at a time. A token the previous
            # one never claimed must not bind into this one.
            _revoke_unclaimed_launches_locked(launches, safe_body)
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
    *,
    body_stopped: bool = False,
) -> bool:
    """Release this launch capability when no provider body can still use it.

    A claimed launch may be between native-ID claim and final-record publish;
    deleting it while the body runs would erase the only recovery witness. The
    launcher may remove its exact claimed capability after it has positively
    stopped that body. Missing means the hook consumed it or another cleanup won.
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
        if launch["state"] != "reserved" and not (
            body_stopped and launch["state"] == "claimed"
        ):
            return False
        expected.unlink()
        directory_fd = os.open(launches, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True


def revoke_unclaimed_launches(agent_dir: Path, body_id: str) -> int:
    """Remove a body's launch capabilities that no provider has claimed.

    For when the caller knows no process of that body can still present
    them: the launcher never ran, or the generation it started has ended.
    """

    safe_body = _validate_body_id(body_id)
    with sessions_root_lock(agent_dir, create=True) as sessions:
        return _revoke_unclaimed_launches_locked(_launches_directory(sessions), safe_body)


def _revoke_unclaimed_launches_locked(launches: Path, body_id: str) -> int:
    removed = 0
    for path in launches.glob("*.json"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            launch = _validate_launch(json.loads(path.read_text(encoding="utf-8")), path)
        except (OSError, json.JSONDecodeError, SessionStateError):
            continue
        if launch["body_id"] == body_id and launch["state"] == "reserved":
            path.unlink()
            removed += 1
    return removed


def record_lane_foreground(
    agent_dir: Path,
    body_id: str,
    *,
    generation: int,
    foreground_process_group: int,
    now: Callable[[], str] = _timestamp,
) -> ManagedLane:
    """Publish the process group a dispatched generation runs as.

    The provider's hook may already have bound this generation, so the lane
    keeps whatever identity it holds and only becomes provider_running.
    """

    if foreground_process_group <= 1:
        raise SessionStateError("managed lane foreground process group is invalid")
    timestamp = now()
    _parse_timestamp(timestamp, field="updated_at")
    with sessions_root_lock(agent_dir, create=False) as sessions:
        path = _lane_path(_lanes_directory(sessions), body_id)
        if path.is_symlink() or not path.is_file():
            raise SessionStateError(f"lane not found: {body_id}")
        current = _validate_lane(json.loads(path.read_text(encoding="utf-8")), path)
        if current["generation"] != generation or current["state"] not in {
            "launch_reserved",
            "relaunching",
            "provider_running",
        }:
            raise SessionStateError(
                f"managed lane {body_id} moved on (generation {current['generation']}, "
                f"{current['state']}) before generation {generation} started"
            )
        updated = dict(
            current,
            state="provider_running",
            foreground_process_group=foreground_process_group,
            updated_at=timestamp,
        )
        _validate_lane(updated, path)
        _atomic_json(path, updated)
        return ManagedLane(path, updated)


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
            # A hook firing again for a conversation this body already runs.
            # A stopped one is being resumed: a lane's shell outlives its
            # agents, so the same body can host it again and must claim anew.
            if (
                existing.get("managed") is True
                and existing.get("state") == "running"
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
                        _finalize_lane_binding_locked(
                            sessions,
                            body_id=body_id,
                            key=key,
                            bound_at=current_text,
                        )
                        path.unlink()
                else:
                    lane_path = _lane_path(_lanes_directory(sessions), body_id)
                    if lane_path.is_file() and not lane_path.is_symlink():
                        lane = _validate_lane(
                            json.loads(lane_path.read_text(encoding="utf-8")), lane_path
                        )
                        if (lane["provider"], lane["session_id"]) != (
                            key.provider,
                            key.session_id,
                        ):
                            _finalize_lane_binding_locked(
                                sessions,
                                body_id=body_id,
                                key=key,
                                bound_at=current_text,
                            )
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
        _finalize_lane_binding_locked(
            sessions,
            body_id=body_id,
            key=key,
            bound_at=current_text,
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
