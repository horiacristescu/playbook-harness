"""Persistent, ownership-aware agent execution over tmux.

The module is intentionally stdlib-only.  tmux is an optional external transport;
project initialization and tmux configuration remain outside this module's scope.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping


SCHEMA = 1
OWNER_MARKER = "playbook-tmux-agent/schema-1"
SERVER_OWNER_MARKER = "playbook-tmux-agent-server/schema-1"
DEFAULT_TMUX_SOCKET = "playbook-harness-v1"
PANE_EXIT_RECORD_GRACE = 2.0
READY_STABILITY_INTERVAL = 0.05
DEFAULT_NAMESPACE = "default"
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
KNOWN_AGENTS = frozenset({"claude", "codex", "agy", "pi", "omp", "command"})
ENVIRONMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class TmuxAgentError(RuntimeError):
    """Expected user-visible transport error."""


class IdentityExistsError(TmuxAgentError):
    """Raised when a logical run identity has already been reserved."""


def validate_identifier(value: str, *, label: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise TmuxAgentError(
            f"invalid {label} {value!r}: use 1-64 letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return value


def default_state_root(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    base = values.get("XDG_STATE_HOME")
    if base:
        return Path(base).expanduser() / "playbook-harness" / "tmux"
    return Path(values.get("HOME", str(Path.home()))).expanduser() / ".local" / "state" / "playbook-harness" / "tmux"


def ensure_state_root(value: str | os.PathLike[str] | None = None) -> Path:
    candidate = Path(value).expanduser() if value is not None else default_state_root()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if candidate.is_symlink():
        raise TmuxAgentError(f"state directory may not be a symlink: {candidate}")
    candidate.mkdir(parents=True, mode=0o700, exist_ok=True)
    if candidate.is_symlink() or not candidate.is_dir():
        raise TmuxAgentError(f"state directory must be a real directory: {candidate}")
    return candidate.resolve()


def tmux_session_name(namespace: str, name: str) -> str:
    validate_identifier(namespace, label="namespace")
    validate_identifier(name, label="name")
    digest = hashlib.sha256(f"{namespace}\0{name}".encode("utf-8")).hexdigest()[:16]
    readable = re.sub(r"[^A-Za-z0-9]", "-", f"{namespace}-{name}")[:32].strip("-")
    return f"pbta-{readable or 'run'}-{digest}"


def resolve_agent_command(
    agent: str,
    agent_args: list[str] | tuple[str, ...],
    *,
    model: str | None = None,
) -> list[str]:
    """Translate one public agent name into an exact argv array."""

    if agent not in KNOWN_AGENTS:
        choices = ", ".join(sorted(KNOWN_AGENTS))
        raise TmuxAgentError(f"unknown agent {agent!r}; choose one of: {choices}")
    args = list(agent_args)
    if any("\0" in value for value in args) or (model is not None and "\0" in model):
        raise TmuxAgentError("agent arguments may not contain NUL bytes")
    if model == "":
        raise TmuxAgentError("model may not be empty")

    if agent == "command":
        if model is not None:
            raise TmuxAgentError("--model is not supported with agent 'command'")
        if not args:
            raise TmuxAgentError("agent 'command' requires argv after '--'")
        return args

    executable = {
        "claude": "claude",
        "codex": "pb-codex",
        "agy": "pb-agy",
        "pi": "pb-pi",
        "omp": "omp",
    }[agent]
    if model is None:
        return [executable, *args]
    if agent == "agy":
        raise TmuxAgentError(
            "--model is not supported for agy because its model CLI contract is unverified; "
            "pass an explicit agent argument instead"
        )
    if agent == "pi":
        return [executable, model, *args]
    return [executable, "--model", model, *args]


def parse_environment(values: list[str] | tuple[str, ...]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or not ENVIRONMENT_RE.fullmatch(key):
            raise TmuxAgentError(f"invalid environment assignment {item!r}; expected KEY=VALUE")
        if "\0" in value:
            raise TmuxAgentError(f"environment value for {key} may not contain NUL bytes")
        parsed[key] = value
    return parsed


def utc_timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TmuxClient:
    def __init__(
        self, binary: str = "tmux", socket_name: str | None = DEFAULT_TMUX_SOCKET
    ) -> None:
        self.binary = binary
        self.socket_name = socket_name

    def command(self, arguments: list[str]) -> list[str]:
        prefix = [self.binary]
        if self.socket_name is not None:
            prefix.extend(["-L", self.socket_name])
        return [*prefix, *arguments]

    def require(self, consumer: str = "pb-tmux-agent") -> None:
        if shutil.which(self.binary) is None:
            raise TmuxAgentError(
                f"tmux is required for {consumer} but was not found on PATH. "
                "Ask the user for permission to install tmux with the machine's "
                "package manager, then retry the same command. "
                "macOS/Homebrew: `brew install tmux`; "
                "Debian/Ubuntu: `sudo apt-get install tmux`."
            )

    def run(
        self,
        arguments: list[str],
        *,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = subprocess.run(
                self.command(arguments),
                input=input_bytes,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise TmuxAgentError(f"could not run tmux: {exc}") from exc
        if check and completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise TmuxAgentError(detail or f"tmux command failed: {' '.join(arguments[:2])}")
        return completed

    def output(self, arguments: list[str]) -> str:
        return self.run(arguments).stdout.decode("utf-8", errors="replace").rstrip("\n")

    def has_session(self, session: str) -> bool:
        completed = self.run(["has-session", "-t", session], check=False)
        if completed.returncode == 0:
            return True
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        absent_markers = (
            "can't find session",
            "no current target",
            "no server running",
            "no such file or directory",
            "connection refused",
        )
        if any(marker in detail.casefold() for marker in absent_markers):
            return False
        raise TmuxAgentError(
            detail or f"tmux could not determine whether session exists: {session}"
        )


def _resolved_cwd(value: str | os.PathLike[str] | None) -> Path:
    candidate = Path.cwd() if value is None else Path(value).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise TmuxAgentError(f"working directory is unavailable: {candidate}: {exc}") from exc
    if not resolved.is_dir():
        raise TmuxAgentError(f"working directory is not a directory: {resolved}")
    return resolved


def _tmux_option(client: TmuxClient, session: str, name: str, value: str) -> None:
    client.run(["set-option", "-t", session, name, value])


def _server_owner(client: TmuxClient) -> str | None:
    completed = client.run(
        ["show-option", "-gv", "@playbook_server_owner"], check=False
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.decode("utf-8", errors="replace").strip()
    return value or None


def require_owned_server(client: TmuxClient) -> None:
    if _server_owner(client) != SERVER_OWNER_MARKER:
        raise TmuxAgentError("tmux server ownership mismatch; refusing operation")


def _prepare_owned_server(client: TmuxClient) -> bool:
    # `list-sessions` is not a server-existence probe: with `exit-empty off`,
    # tmux keeps the server alive after its final session exits and reports
    # `no current target`.  A global option query remains valid in that state.
    completed = client.run(["show-options", "-g"], check=False)
    exists = completed.returncode == 0
    if not exists:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        absent_markers = (
            "no server running",
            "no such file or directory",
            "connection refused",
        )
        if not any(marker in detail.casefold() for marker in absent_markers):
            raise TmuxAgentError(
                detail or "tmux could not determine whether its server exists"
            )
    if exists:
        require_owned_server(client)
    return exists


def _configure_owned_server(client: TmuxClient, *, already_owned: bool) -> None:
    if not already_owned:
        client.run(["set-option", "-g", "@playbook_server_owner", SERVER_OWNER_MARKER])
    require_owned_server(client)
    client.run(["set-option", "-s", "exit-empty", "off"])
    client.run(["set-option", "-g", "mouse", "on"])
    client.run(
        [
            "bind-key",
            "-n",
            "WheelUpPane",
            "if-shell",
            "-F",
            "#{pane_in_mode}",
            "send-keys -X -N 3 scroll-up",
            "copy-mode -e ; send-keys -X -N 3 scroll-up",
        ]
    )
    client.run(
        [
            "bind-key",
            "-n",
            "WheelDownPane",
            "if-shell",
            "-F",
            "#{pane_in_mode}",
            "send-keys -X -N 3 scroll-down",
            "select-pane -t =",
        ]
    )


def _pane_fields(client: TmuxClient, session: str) -> dict[str, Any]:
    format_string = "|".join(
        [
            "#{pane_id}",
            "#{pane_pid}",
            "#{pane_dead}",
            "#{pane_dead_status}",
            "#{session_activity}",
            "#{pane_tty}",
            "#{pane_dead_time}",
        ]
    )
    values = client.output(["display-message", "-p", "-t", session, format_string]).split("|")
    if len(values) != 7:
        raise TmuxAgentError(f"tmux returned malformed pane state for {session}")
    return {
        "pane_id": values[0],
        "pane_pid": int(values[1]),
        "pane_dead": values[2] == "1",
        "pane_dead_status": int(values[3]) if values[3] else None,
        "activity_epoch": int(values[4]),
        "pane_tty": values[5],
        "pane_dead_epoch": int(values[6]) if values[6] else None,
    }


def require_flat_topology(
    client: TmuxClient, session: str, *, expected_pane: str | None = None
) -> str:
    windows = client.output(
        ["list-windows", "-t", session, "-F", "#{window_id}|#{window_panes}"]
    ).splitlines()
    panes = client.output(
        ["list-panes", "-s", "-t", session, "-F", "#{pane_id}"]
    ).splitlines()
    if len(windows) != 1 or not windows[0].endswith("|1") or len(panes) != 1:
        raise TmuxAgentError(
            f"owned tmux topology must be exactly one window and one pane: {session}"
        )
    pane = panes[0]
    if expected_pane is not None and pane != expected_pane:
        raise TmuxAgentError(f"owned tmux pane identity changed: {session}")
    return pane


def load_run(
    *,
    name: str,
    namespace: str = DEFAULT_NAMESPACE,
    state_dir: str | os.PathLike[str] | None = None,
) -> tuple[RunPaths, dict[str, Any]]:
    paths = RunPaths.resolve(state_dir=state_dir, namespace=namespace, name=name)
    metadata = read_json(paths.meta, kind="run metadata")
    expected = {
        "owner": OWNER_MARKER,
        "namespace": namespace,
        "name": name,
        "tmux_session": tmux_session_name(namespace, name),
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise TmuxAgentError(f"run metadata ownership mismatch for {namespace}/{name}: {key}")
    return paths, metadata


def require_owned_session(
    metadata: Mapping[str, Any], client: TmuxClient | None = None
) -> tuple[TmuxClient, str]:
    tmux = TmuxClient() if client is None else client
    tmux.require()
    require_owned_server(tmux)
    session = metadata.get("tmux_session")
    if not isinstance(session, str) or not tmux.has_session(session):
        raise TmuxAgentError(f"owned tmux session is unavailable: {session or '(missing)'}")
    owner = tmux.output(["show-option", "-v", "-t", session, "@playbook_owner"])
    identity = tmux.output(["show-option", "-v", "-t", session, "@playbook_identity"])
    expected_identity = f"{metadata['namespace']}/{metadata['name']}"
    if owner != OWNER_MARKER or identity != expected_identity:
        raise TmuxAgentError(f"tmux session ownership mismatch: {session}")
    expected_pane = metadata.get("pane_id")
    require_flat_topology(
        tmux,
        session,
        expected_pane=expected_pane if isinstance(expected_pane, str) else None,
    )
    return tmux, session


def send_message(
    *,
    name: str,
    message: str,
    namespace: str = DEFAULT_NAMESPACE,
    state_dir: str | os.PathLike[str] | None = None,
    enter: bool = True,
    client: TmuxClient | None = None,
) -> None:
    if "\0" in message:
        raise TmuxAgentError("message may not contain NUL bytes")
    unsupported_controls = sorted(
        {ord(character) for character in message if (ord(character) < 32 and character not in "\n\t") or ord(character) == 127}
    )
    if unsupported_controls:
        rendered = ", ".join(f"0x{value:02x}" for value in unsupported_controls)
        raise TmuxAgentError(
            f"message contains terminal control bytes that cannot be preserved literally: {rendered}"
        )
    paths, metadata = load_run(name=name, namespace=namespace, state_dir=state_dir)
    tmux, session = require_owned_session(metadata, client)
    pane_state = _pane_fields(tmux, session)
    if pane_state["pane_dead"]:
        raise TmuxAgentError(f"cannot send to dead tmux pane: {session}")
    pane = metadata.get("pane_id")
    target = pane if isinstance(pane, str) and pane else session
    buffer_name = f"pbta-{os.getpid()}-{uuid.uuid4().hex[:12]}"
    lock_fd = os.open(paths.run_dir, os.O_RDONLY)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        commands = [
            "load-buffer",
            "-b",
            buffer_name,
            "-",
            ";",
            "paste-buffer",
            "-p",
            "-d",
            "-b",
            buffer_name,
            "-t",
            target,
        ]
        tmux.run(commands, input_bytes=message.encode("utf-8"))
        if enter:
            # Full-screen editors such as Claude Code consume bracketed paste
            # asynchronously. An Enter in the same tmux command can arrive
            # before the editor has closed the paste transaction and becomes
            # another newline in the buffer. Submit as a later terminal event.
            time.sleep(0.1)
            tmux.run(["send-keys", "-t", target, "Enter"])
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def label_run(
    *,
    name: str,
    label: str,
    namespace: str = DEFAULT_NAMESPACE,
    state_dir: str | os.PathLike[str] | None = None,
    client: TmuxClient | None = None,
) -> None:
    """Expose a safe human label without changing the durable run identity."""

    validate_identifier(label, label="session label")
    _, metadata = load_run(name=name, namespace=namespace, state_dir=state_dir)
    tmux, session = require_owned_session(metadata, client)
    tmux.run(["set-option", "-t", session, "status-left", f"#[bold] {label} #[default]"])
    tmux.run(["set-option", "-t", session, "status-left-length", "72"])
    tmux.run(["set-window-option", "-t", f"{session}:0", "automatic-rename", "off"])
    tmux.run(["rename-window", "-t", f"{session}:0", label])


def peek_run(
    *,
    name: str,
    lines: int = 50,
    namespace: str = DEFAULT_NAMESPACE,
    state_dir: str | os.PathLike[str] | None = None,
    client: TmuxClient | None = None,
) -> str:
    if lines < 1 or lines > 10_000:
        raise TmuxAgentError("lines must be between 1 and 10000")
    _, metadata = load_run(name=name, namespace=namespace, state_dir=state_dir)
    tmux, session = require_owned_session(metadata, client)
    pane = str(metadata["pane_id"])
    return tmux.output(["capture-pane", "-p", "-t", pane, "-S", f"-{lines}"])


def detach_run(
    *,
    name: str,
    namespace: str = DEFAULT_NAMESPACE,
    state_dir: str | os.PathLike[str] | None = None,
    client: TmuxClient | None = None,
) -> None:
    _, metadata = load_run(name=name, namespace=namespace, state_dir=state_dir)
    tmux, session = require_owned_session(metadata, client)
    tmux.run(["detach-client", "-s", session], check=False)


def attach_run(
    *,
    name: str,
    namespace: str = DEFAULT_NAMESPACE,
    state_dir: str | os.PathLike[str] | None = None,
    client: TmuxClient | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    values = os.environ if environment is None else environment
    if values.get("TMUX"):
        raise TmuxAgentError(
            "refusing nested tmux attach; run attach from an ordinary terminal"
        )
    _, metadata = load_run(name=name, namespace=namespace, state_dir=state_dir)
    tmux, session = require_owned_session(metadata, client)
    completed = subprocess.run(
        tmux.command(["attach-session", "-t", session]), check=False
    )
    if completed.returncode != 0:
        raise TmuxAgentError(f"could not attach to tmux session: {session}")
    return completed.returncode


def read_log(
    *,
    name: str,
    namespace: str = DEFAULT_NAMESPACE,
    state_dir: str | os.PathLike[str] | None = None,
) -> str:
    paths, _ = load_run(name=name, namespace=namespace, state_dir=state_dir)
    try:
        return paths.log.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise TmuxAgentError(f"terminal log is unreadable: {paths.log}: {exc}") from exc


def tail_log(
    *,
    name: str,
    lines: int = 50,
    namespace: str = DEFAULT_NAMESPACE,
    state_dir: str | os.PathLike[str] | None = None,
) -> str:
    if lines < 1 or lines > 10_000:
        raise TmuxAgentError("lines must be between 1 and 10000")
    paths, _ = load_run(name=name, namespace=namespace, state_dir=state_dir)
    try:
        with paths.log.open("r", encoding="utf-8", errors="replace") as handle:
            return "".join(deque(handle, maxlen=lines))
    except OSError as exc:
        raise TmuxAgentError(f"terminal log is unreadable: {paths.log}: {exc}") from exc


def list_runs(
    *,
    namespace: str | None = None,
    state_dir: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    root = ensure_state_root(state_dir)
    runs: list[dict[str, Any]] = []
    for paths in iter_run_paths(root, namespace):
        try:
            metadata = read_json(paths.meta, kind="run metadata")
        except TmuxAgentError:
            continue
        if (
            metadata.get("owner") == OWNER_MARKER
            and metadata.get("namespace") == paths.namespace
            and metadata.get("name") == paths.name
            and metadata.get("tmux_session") == tmux_session_name(paths.namespace, paths.name)
        ):
            runs.append(metadata)
    return runs


def _normal_exit_code(value: Any) -> int:
    if not isinstance(value, int):
        return 1
    if value < 0:
        return min(255, 128 + abs(value))
    return min(255, value)


def status_run(
    *,
    name: str,
    namespace: str = DEFAULT_NAMESPACE,
    state_dir: str | os.PathLike[str] | None = None,
    client: TmuxClient | None = None,
) -> dict[str, Any]:
    paths, metadata = load_run(name=name, namespace=namespace, state_dir=state_dir)
    tmux = TmuxClient() if client is None else client
    result: dict[str, Any] | None = None
    if paths.result.exists():
        result = read_json(paths.result, kind="run result")

    session = str(metadata["tmux_session"])
    pane: dict[str, Any] | None = None
    terminal_state = "missing"
    if tmux.has_session(session):
        require_owned_session(metadata, tmux)
        pane = _pane_fields(tmux, session)
        terminal_state = "dead" if pane["pane_dead"] else "active"

    # The runner publishes result.json before returning, but it can do so after
    # this observer's first exists() check and before tmux reports the pane dead.
    # Recheck at that boundary so a completed run never transiently becomes lost.
    if result is None and (pane is None or pane["pane_dead"]) and paths.result.exists():
        result = read_json(paths.result, kind="run result")

    if result is not None:
        state = result.get("state")
    elif pane is None:
        try:
            _owned_runner_without_tmux(paths, metadata)
        except TmuxAgentError:
            state = "lost"
        else:
            state = "orphaned"
    elif pane["pane_dead"]:
        dead_epoch = pane.get("pane_dead_epoch")
        if (
            isinstance(dead_epoch, int)
            and time.time() - dead_epoch < PANE_EXIT_RECORD_GRACE
        ):
            state = "recording_exit"
        else:
            state = "lost"
    else:
        state = metadata.get("state", "starting")
        if state not in {"starting", "running", "finishing"}:
            state = "running"

    status = dict(metadata)
    status.update(
        {
            "state": state,
            "terminal_state": terminal_state,
            "result_available": result is not None,
            "exit_status": result.get("exit_status") if result else None,
            "finished_at": result.get("finished_at") if result else None,
        }
    )
    if pane:
        status.update(pane)
    return status


def result_run(
    *,
    name: str,
    namespace: str = DEFAULT_NAMESPACE,
    state_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    paths, _ = load_run(name=name, namespace=namespace, state_dir=state_dir)
    return read_json(paths.result, kind="run result")


def wait_run(
    *,
    name: str,
    namespace: str = DEFAULT_NAMESPACE,
    state_dir: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
    client: TmuxClient | None = None,
) -> tuple[dict[str, Any], int]:
    if timeout is not None and timeout < 0:
        raise TmuxAgentError("timeout must be non-negative")
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        status = status_run(
            name=name,
            namespace=namespace,
            state_dir=state_dir,
            client=client,
        )
        if status["state"] in {"completed", "failed", "stopped", "lost"}:
            if status["state"] == "lost":
                return status, 1
            return status, _normal_exit_code(status.get("exit_status"))
        if deadline is not None and time.monotonic() >= deadline:
            status["observer_timeout"] = True
            return status, 124
        time.sleep(0.05)


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        try:
            output = subprocess.check_output(
                ["ps", "-axo", "pgid="], text=True, stderr=subprocess.DEVNULL
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise TmuxAgentError(
                f"cannot inspect owned process group {process_group}: {exc}"
            ) from exc
        return any(field.strip() == str(process_group) for field in output.splitlines())
    return True


def _owned_runner_without_tmux(paths: RunPaths, metadata: Mapping[str, Any]) -> int:
    """Authenticate the still-live runner when its tmux session disappeared."""

    runner_pid = metadata.get("runner_pid")
    runner_group = metadata.get("runner_process_group")
    terminal_session = metadata.get("terminal_session")
    if (
        not isinstance(runner_pid, int)
        or runner_pid <= 1
        or runner_pid != metadata.get("pane_pid")
        or runner_group != runner_pid
        or not isinstance(terminal_session, int)
    ):
        raise TmuxAgentError(
            "owned tmux session is unavailable and runner identity is incomplete"
        )
    try:
        if os.getpgid(runner_pid) != runner_group or os.getsid(runner_pid) != terminal_session:
            raise TmuxAgentError(
                "owned runner process identity changed after tmux session loss"
            )
        command = subprocess.check_output(
            ["ps", "-ww", "-p", str(runner_pid), "-o", "command="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except ProcessLookupError as exc:
        raise TmuxAgentError(
            "owned runner is no longer alive after tmux session loss"
        ) from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise TmuxAgentError(
            f"cannot authenticate owned runner after tmux session loss: {exc}"
        ) from exc
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise TmuxAgentError(
            "owned runner command is malformed after tmux session loss"
        ) from exc
    expected = [
        str(Path(__file__).resolve()),
        "_run",
        str(paths.meta.resolve()),
        str(paths.configured.resolve()),
        str(paths.ready.resolve()),
    ]
    if len(argv) < len(expected) or argv[-len(expected):] != expected:
        raise TmuxAgentError(
            "owned runner command changed after tmux session loss; refusing stop"
        )
    return runner_pid


def _terminate_process_group(process_group: int, *, grace: float) -> None:
    """Boundedly terminate one group while its runner still proves ownership."""

    if not _process_group_exists(process_group):
        return
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while _process_group_exists(process_group) and time.monotonic() < deadline:
        time.sleep(0.02)
    if _process_group_exists(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            return
    kill_deadline = time.monotonic() + 2.0
    while _process_group_exists(process_group) and time.monotonic() < kill_deadline:
        time.sleep(0.02)
    if _process_group_exists(process_group):
        raise TmuxAgentError(f"owned process group survived termination: {process_group}")


def stop_run(
    *,
    name: str,
    namespace: str = DEFAULT_NAMESPACE,
    state_dir: str | os.PathLike[str] | None = None,
    grace: float = 2.0,
    client: TmuxClient | None = None,
) -> dict[str, Any]:
    if grace < 0:
        raise TmuxAgentError("grace must be non-negative")
    paths, metadata = load_run(name=name, namespace=namespace, state_dir=state_dir)
    tmux = TmuxClient() if client is None else client
    session = str(metadata["tmux_session"])

    if paths.result.exists():
        result = read_json(paths.result, kind="run result")
        if result.get("phase") == "runner":
            if tmux.has_session(session):
                require_owned_session(metadata, tmux)
            raise TmuxAgentError(
                "runner failed before cleanup proof; retained pane and process-group evidence"
            )
        if tmux.has_session(session):
            tmux, session = require_owned_session(metadata, tmux)
            tmux.run(["kill-session", "-t", session])
        return result

    owned_session = tmux.has_session(session)
    if owned_session:
        tmux, session = require_owned_session(metadata, tmux)
        pane = _pane_fields(tmux, session)
        runner_pid = metadata.get("pane_pid")
        if (
            not isinstance(runner_pid, int)
            or runner_pid <= 1
            or runner_pid != pane["pane_pid"]
            or pane["pane_dead"]
        ):
            raise TmuxAgentError(
                "owned runner identity is unavailable or changed; refusing stop"
            )
    else:
        runner_pid = _owned_runner_without_tmux(paths, metadata)

    atomic_write_json(
        paths.run_dir / "stop.request.json",
        {"schema": SCHEMA, "requested_at": utc_timestamp(), "grace": grace},
    )
    try:
        os.kill(runner_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    # Only the live runner may signal its foreground process group. The
    # controller authenticates and nudges the runner PID, then waits for the
    # runner to clean the group and publish a terminal result.
    result_deadline = time.monotonic() + grace + 3.0
    while not paths.result.exists() and time.monotonic() < result_deadline:
        time.sleep(0.02)
    if not paths.result.exists():
        raise TmuxAgentError(
            "owned runner did not publish a stop result; retained tmux state for diagnosis"
        )
    if owned_session:
        tmux.run(["kill-session", "-t", session], check=False)
    return read_json(paths.result, kind="run result")


def _write_start_failure(paths: RunPaths, message: str) -> None:
    publish_json_once(
        paths.result,
        {
            "schema": SCHEMA,
            "state": "failed",
            "phase": "start",
            "error": message,
            "finished_at": utc_timestamp(),
            "exit_status": None,
        },
    )


def _pane_exit_hook(meta_path: Path) -> str:
    command = shlex.join(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "_pane_exit",
            str(meta_path),
            "#{pane_id}",
            "#{pane_dead_status}",
            "#{pane_dead_signal}",
        ]
    )
    return f"run-shell {shlex.quote(command)}"


def _pane_exit_main(
    meta_path: Path, pane_id: str, status_text: str, signal_text: str
) -> int:
    paths = RunPaths(
        root=meta_path.parent.parent.parent,
        namespace=meta_path.parent.parent.name,
        name=meta_path.parent.name,
    )
    metadata = read_json(meta_path, kind="metadata")
    if metadata.get("owner") != OWNER_MARKER or metadata.get("pane_id") != pane_id:
        return 126
    runner_status = int(status_text) if status_text else None
    runner_signal = signal_text or None
    signal_number = None
    if runner_signal is not None:
        candidate = getattr(signal, f"SIG{runner_signal.upper()}", None)
        if isinstance(candidate, int):
            signal_number = candidate
    exit_status = -signal_number if signal_number is not None else runner_status
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "state": "failed",
        "phase": "runner",
        "error": "tmux runner exited before publishing its terminal result",
        "finished_at": utc_timestamp(),
        "exit_status": exit_status,
        "runner_exit_status": runner_status,
        "runner_exit_signal": runner_signal,
    }
    for key in ("runner_pid", "child_pid", "process_group"):
        if key in metadata:
            result[key] = metadata[key]
    publish_json_once(paths.result, result)
    return 0


def _terminal_foreground_group(fd: int = 0) -> int:
    try:
        return os.tcgetpgrp(fd)
    except OSError as exc:
        raise TmuxAgentError(f"runner has no usable controlling terminal: {exc}") from exc


def _set_terminal_foreground_group(process_group: int, fd: int = 0) -> None:
    previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(fd, process_group)
    except OSError as exc:
        raise TmuxAgentError(
            f"could not give the pane terminal to process group {process_group}: {exc}"
        ) from exc
    finally:
        signal.signal(signal.SIGTTOU, previous)


def _exec_after_foreground(start_fd: int, status_fd: int, command: list[str]) -> int:
    """Wait until the runner owns the foreground handoff, then replace this shim."""

    try:
        started = os.read(start_fd, 1)
    finally:
        os.close(start_fd)
    if started != b"1":
        return 125
    os.set_inheritable(status_fd, False)
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        try:
            os.write(status_fd, str(exc).encode("utf-8", errors="replace"))
        finally:
            os.close(status_fd)
        print(f"agent launch failed: {exc}", file=sys.stderr, flush=True)
        return 127


def start_run(
    *,
    name: str,
    agent: str,
    agent_args: list[str] | tuple[str, ...],
    namespace: str = DEFAULT_NAMESPACE,
    state_dir: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    environment: Mapping[str, str] | None = None,
    model: str | None = None,
    client: TmuxClient | None = None,
) -> dict[str, Any]:
    tmux = TmuxClient() if client is None else client
    tmux.require()
    paths = RunPaths.resolve(state_dir=state_dir, namespace=namespace, name=name)
    working_directory = _resolved_cwd(cwd)
    command = resolve_agent_command(agent, agent_args, model=model)
    overrides = dict(environment or {})
    for key, value in overrides.items():
        if not ENVIRONMENT_RE.fullmatch(key) or "\0" in value:
            raise TmuxAgentError(f"invalid environment override: {key!r}")

    server_lock_fd = os.open(paths.root, os.O_RDONLY)
    fcntl.flock(server_lock_fd, fcntl.LOCK_EX)
    try:
        server_already_owned = _prepare_owned_server(tmux)
        reserve_run(paths)
    except Exception:
        fcntl.flock(server_lock_fd, fcntl.LOCK_UN)
        os.close(server_lock_fd)
        raise
    try:
        paths.log.touch(mode=0o600, exist_ok=False)
        session = tmux_session_name(namespace, name)
        metadata: dict[str, Any] = {
            "schema": SCHEMA,
            "owner": OWNER_MARKER,
            "namespace": namespace,
            "name": name,
            "tmux_session": session,
            "command": command,
            "agent": agent,
            "model": model,
            "cwd": str(working_directory),
            "environment_keys": sorted(overrides),
            "started_at": utc_timestamp(),
            "state": "starting",
        }
        atomic_write_json(paths.meta, metadata)
    except Exception:
        fcntl.flock(server_lock_fd, fcntl.LOCK_UN)
        os.close(server_lock_fd)
        raise

    if tmux.has_session(session):
        message = f"tmux session name collision: {session}"
        _write_start_failure(paths, message)
        fcntl.flock(server_lock_fd, fcntl.LOCK_UN)
        os.close(server_lock_fd)
        raise IdentityExistsError(message)

    runner = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_run",
        str(paths.meta),
        str(paths.configured),
        str(paths.ready),
    ]
    session_created = False
    setup_complete = False
    try:
        new_session = ["new-session", "-d", "-s", session, "-c", str(working_directory)]
        inherited_path = os.environ.get("PATH")
        if inherited_path is not None:
            new_session.extend(["-e", f"PATH={inherited_path}"])
        for key, value in overrides.items():
            new_session.extend(["-e", f"{key}={value}"])
        new_session.extend(["--", *runner])
        tmux.run(new_session)
        session_created = True
        _configure_owned_server(tmux, already_owned=server_already_owned)
        fcntl.flock(server_lock_fd, fcntl.LOCK_UN)
        os.close(server_lock_fd)
        server_lock_fd = -1
        _tmux_option(tmux, session, "remain-on-exit", "on")
        _tmux_option(tmux, session, "@playbook_owner", OWNER_MARKER)
        _tmux_option(tmux, session, "@playbook_identity", f"{namespace}/{name}")
        _tmux_option(tmux, session, "@playbook_state_dir", str(paths.run_dir))
        tmux.run(["pipe-pane", "-t", session, "-o", f"cat >> {shlex.quote(str(paths.log))}"])
        require_flat_topology(tmux, session)
        metadata.update(_pane_fields(tmux, session))
        atomic_write_json(paths.meta, metadata)
        tmux.run(["set-hook", "-t", session, "pane-died", _pane_exit_hook(paths.meta)])
        paths.configured.touch(mode=0o600, exist_ok=False)
        setup_complete = True

        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if paths.ready.exists():
                running = read_json(paths.meta, kind="run metadata")
                if running.get("state") != "running":
                    raise TmuxAgentError("runner readiness disagrees with durable state")
                require_owned_session(running, tmux)
                return running
            if paths.result.exists():
                result = read_json(paths.result, kind="run result")
                if result.get("phase") in {"start", "runner"}:
                    detail = result.get("error") or (
                        f"runner exited before readiness: state={result.get('state')} "
                        f"exit={result.get('exit_status')}"
                    )
                    raise TmuxAgentError(str(detail))
                return status_run(
                    name=name,
                    namespace=namespace,
                    state_dir=paths.root,
                    client=tmux,
                )
            time.sleep(0.01)
        raise TmuxAgentError("runner did not become ready within 30 seconds")
    except Exception as exc:
        message = str(exc)
        if server_lock_fd >= 0:
            fcntl.flock(server_lock_fd, fcntl.LOCK_UN)
            os.close(server_lock_fd)
        if session_created and not setup_complete:
            tmux.run(["kill-session", "-t", session], check=False)
        _write_start_failure(paths, message)
        raise


def _runner_main(meta_path: Path, configured_path: Path, ready_path: Path) -> int:
    paths = RunPaths(
        root=meta_path.parent.parent.parent,
        namespace=meta_path.parent.parent.name,
        name=meta_path.parent.name,
    )
    deadline = time.monotonic() + 30.0
    while not configured_path.exists():
        if time.monotonic() >= deadline:
            _write_start_failure(paths, "runner handshake timed out")
            return 125
        time.sleep(0.01)

    metadata = read_json(meta_path, kind="metadata")
    command = metadata.get("command")
    cwd = metadata.get("cwd")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        _write_start_failure(paths, "metadata command is invalid")
        return 125
    if not isinstance(cwd, str):
        _write_start_failure(paths, "metadata cwd is invalid")
        return 125

    try:
        runner_process_group = os.getpgrp()
        terminal_session = os.getsid(0)
        original_foreground_group = _terminal_foreground_group()
    except (OSError, TmuxAgentError) as exc:
        _write_start_failure(paths, str(exc))
        return 125

    start_read_fd, start_write_fd = os.pipe()
    status_read_fd, status_write_fd = os.pipe()
    child: subprocess.Popen[bytes] | None = None
    try:
        shim = [
            sys.executable,
            str(Path(__file__).resolve()),
            "_exec",
            str(start_read_fd),
            str(status_write_fd),
            *command,
        ]
        child = subprocess.Popen(
            shim,
            cwd=cwd,
            preexec_fn=os.setpgrp,
            pass_fds=(start_read_fd, status_write_fd),
        )
        os.close(start_read_fd)
        start_read_fd = -1
        os.close(status_write_fd)
        status_write_fd = -1
        _set_terminal_foreground_group(child.pid)
        os.write(start_write_fd, b"1")
        os.close(start_write_fd)
        start_write_fd = -1
        readable, _, _ = select.select([status_read_fd], [], [], 5.0)
        if not readable:
            raise TmuxAgentError("agent exec handshake timed out")
        exec_error = os.read(status_read_fd, 4096)
        if exec_error:
            raise TmuxAgentError(exec_error.decode("utf-8", errors="replace"))
    except (OSError, TmuxAgentError) as exc:
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        _write_start_failure(paths, f"agent launch failed: {exc}")
        return 127
    finally:
        if start_read_fd >= 0:
            os.close(start_read_fd)
        if start_write_fd >= 0:
            os.close(start_write_fd)
        if status_write_fd >= 0:
            os.close(status_write_fd)
        os.close(status_read_fd)

    termination: dict[str, float | int | None] = {
        "signal": None,
        "at": None,
        "grace": 2.0,
    }

    def forward_termination(signum: int, _frame: Any) -> None:
        if termination["signal"] is None:
            termination["signal"] = signum
            termination["at"] = time.monotonic()
            stop_request = paths.run_dir / "stop.request.json"
            if stop_request.exists():
                request = read_json(stop_request, kind="stop request")
                requested_grace = request.get("grace")
                if isinstance(requested_grace, (int, float)) and requested_grace >= 0:
                    termination["grace"] = float(requested_grace)
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, forward_termination)

    stability_deadline = time.monotonic() + READY_STABILITY_INTERVAL
    while (
        child.poll() is None
        and termination["signal"] is None
        and time.monotonic() < stability_deadline
    ):
        time.sleep(0.005)
    ready_to_publish = child.poll() is None and termination["signal"] is None

    metadata.update(
        {
            "state": "running" if ready_to_publish else "finishing",
            "runner_pid": os.getpid(),
            "runner_process_group": runner_process_group,
            "terminal_session": terminal_session,
            "original_foreground_process_group": original_foreground_group,
            "child_pid": child.pid,
            "process_group": child.pid,
            "foreground_process_group": child.pid,
            "child_started_at": utc_timestamp(),
        }
    )
    atomic_write_json(meta_path, metadata)
    if ready_to_publish and child.poll() is None and termination["signal"] is None:
        ready_path.touch(mode=0o600, exist_ok=False)

    while child.poll() is None:
        if (
            termination["at"] is not None
            and time.monotonic() - float(termination["at"])
            >= float(termination["grace"])
            and _process_group_exists(child.pid)
        ):
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.02)
    return_code = int(child.returncode)
    # The direct child may exit while helpers remain in its group.  Close that
    # owned group before publishing a terminal result; after publication its PGID
    # could eventually be reused, so later controllers must never signal it based
    # only on stale durable metadata.
    _terminate_process_group(child.pid, grace=2.0)
    terminal_restore_error: str | None = None
    try:
        _set_terminal_foreground_group(runner_process_group)
    except TmuxAgentError as exc:
        terminal_restore_error = str(exc)
    stop_requested = (paths.run_dir / "stop.request.json").exists()
    state = (
        "failed"
        if terminal_restore_error is not None
        else ("stopped" if stop_requested else ("completed" if return_code == 0 else "failed"))
    )
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "state": state,
        "finished_at": utc_timestamp(),
        "exit_status": return_code,
        "child_pid": child.pid,
        "process_group": child.pid,
    }
    if terminal_restore_error is not None:
        result["error"] = terminal_restore_error
    publish_json_once(paths.result, result)
    return return_code if 0 <= return_code <= 255 else 1


def _add_common_options(parser: argparse.ArgumentParser, *, include_json: bool = True) -> None:
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE, help="logical namespace")
    parser.add_argument("--state-dir", help="state root (default: XDG state directory)")
    if include_json:
        parser.add_argument("--json", action="store_true", help="emit stable JSON")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pb-tmux-agent",
        description="Run and control persistent, Playbook-owned tmux agents.",
    )
    commands = parser.add_subparsers(dest="command_name", required=True)

    start = commands.add_parser("start", help="start one persistent run")
    start.add_argument("name")
    start.add_argument("agent", choices=sorted(KNOWN_AGENTS))
    _add_common_options(start)
    start.add_argument("--cwd", help="working directory (default: current directory)")
    start.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    start.add_argument("--model")

    send = commands.add_parser("send", help="send literal text to a live run")
    send.add_argument("name")
    _add_common_options(send)
    send.add_argument("--no-enter", action="store_true", help="do not press Enter after paste")
    send.add_argument("message")

    attach = commands.add_parser("attach", help="attach from an ordinary terminal")
    attach.add_argument("name")
    _add_common_options(attach, include_json=False)

    detach = commands.add_parser("detach", help="detach clients from one owned run")
    detach.add_argument("name")
    _add_common_options(detach)

    peek = commands.add_parser("peek", help="capture recent tmux pane text")
    peek.add_argument("name")
    peek.add_argument("lines", nargs="?", type=int, default=50)
    _add_common_options(peek)

    tail = commands.add_parser("tail", help="print the last lines of the durable log")
    tail.add_argument("name")
    tail.add_argument("lines", nargs="?", type=int, default=50)
    _add_common_options(tail)

    log = commands.add_parser("log", help="print the complete durable log")
    log.add_argument("name")
    _add_common_options(log)

    listing = commands.add_parser("list", help="list owned runs")
    _add_common_options(listing)

    status = commands.add_parser("status", help="show current observable state")
    status.add_argument("name")
    _add_common_options(status)

    wait = commands.add_parser("wait", help="wait for termination without imposing a deadline")
    wait.add_argument("name")
    _add_common_options(wait)
    wait.add_argument("--timeout", type=float, help="observer timeout in seconds (exit 124)")

    result = commands.add_parser("result", help="show the durable terminal result")
    result.add_argument("name")
    _add_common_options(result)

    stop = commands.add_parser("stop", help="stop exactly one owned run")
    stop.add_argument("name")
    _add_common_options(stop)
    stop.add_argument("--grace", type=float, default=2.0, help="TERM grace period in seconds")
    return parser


def _emit_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _emit_record(value: Mapping[str, Any], *, as_json: bool) -> None:
    if as_json:
        _emit_json(value)
        return
    identity = f"{value.get('namespace', '?')}/{value.get('name', '?')}"
    state = value.get("state", "unknown")
    detail = f" exit={value['exit_status']}" if value.get("exit_status") is not None else ""
    print(f"{identity} {state}{detail}")


def _public_main(argv: list[str]) -> int:
    agent_args: list[str] = []
    parse_argv = argv
    if argv and argv[0] == "start" and "--" in argv:
        separator = argv.index("--")
        parse_argv = argv[:separator]
        agent_args = argv[separator + 1 :]
    options = _parser().parse_args(parse_argv)
    common = {"namespace": options.namespace, "state_dir": options.state_dir}
    command_name = options.command_name

    if command_name == "start":
        value = start_run(
            name=options.name,
            agent=options.agent,
            agent_args=agent_args,
            cwd=options.cwd,
            environment=parse_environment(options.env),
            model=options.model,
            **common,
        )
        _emit_record(value, as_json=options.json)
        return 0
    if command_name == "send":
        send_message(
            name=options.name,
            message=options.message,
            enter=not options.no_enter,
            **common,
        )
        if options.json:
            _emit_json({"name": options.name, "namespace": options.namespace, "sent": True})
        else:
            print(f"{options.namespace}/{options.name} sent")
        return 0
    if command_name == "attach":
        return attach_run(name=options.name, **common)
    if command_name == "detach":
        detach_run(name=options.name, **common)
        if options.json:
            _emit_json({"name": options.name, "namespace": options.namespace, "detached": True})
        else:
            print(f"{options.namespace}/{options.name} detached")
        return 0
    if command_name in {"peek", "tail", "log"}:
        content = (
            peek_run(name=options.name, lines=options.lines, **common)
            if command_name == "peek"
            else (
                tail_log(name=options.name, lines=options.lines, **common)
                if command_name == "tail"
                else read_log(name=options.name, **common)
            )
        )
        if options.json:
            _emit_json(
                {"name": options.name, "namespace": options.namespace, "log": content}
            )
        else:
            sys.stdout.write(content)
            if command_name == "peek" and content and not content.endswith("\n"):
                sys.stdout.write("\n")
        return 0
    if command_name == "list":
        values = list_runs(namespace=options.namespace, state_dir=options.state_dir)
        if options.json:
            _emit_json(values)
        else:
            for value in values:
                _emit_record(value, as_json=False)
        return 0
    if command_name == "status":
        _emit_record(status_run(name=options.name, **common), as_json=options.json)
        return 0
    if command_name == "wait":
        value, return_code = wait_run(name=options.name, timeout=options.timeout, **common)
        _emit_record(value, as_json=options.json)
        return return_code
    if command_name == "result":
        value = result_run(name=options.name, **common)
        if options.json:
            _emit_json(value)
        else:
            state = value.get("state", "unknown")
            exit_status = value.get("exit_status")
            print(f"{options.namespace}/{options.name} {state} exit={exit_status}")
        return 0
    if command_name == "stop":
        value = stop_run(name=options.name, grace=options.grace, **common)
        if options.json:
            _emit_json(value)
        else:
            print(
                f"{options.namespace}/{options.name} {value.get('state', 'unknown')} "
                f"exit={value.get('exit_status')}"
            )
        return 0
    raise TmuxAgentError(f"unsupported command: {command_name}")


def main(arguments: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if arguments is None else arguments)
    if argv and argv[0] == "_run":
        if len(argv) != 4:
            raise TmuxAgentError("internal runner invocation is invalid")
        return _runner_main(Path(argv[1]), Path(argv[2]), Path(argv[3]))
    if argv and argv[0] == "_exec":
        if len(argv) < 4:
            raise TmuxAgentError("internal foreground exec invocation is invalid")
        try:
            start_fd = int(argv[1])
            status_fd = int(argv[2])
        except ValueError as exc:
            raise TmuxAgentError("internal foreground exec fd is invalid") from exc
        return _exec_after_foreground(start_fd, status_fd, argv[3:])
    if argv and argv[0] == "_pane_exit":
        if len(argv) != 5:
            raise TmuxAgentError("internal pane-exit invocation is invalid")
        return _pane_exit_main(Path(argv[1]), argv[2], argv[3], argv[4])
    return _public_main(argv)


@dataclass(frozen=True)
class RunPaths:
    root: Path
    namespace: str
    name: str

    @classmethod
    def resolve(
        cls,
        *,
        state_dir: str | os.PathLike[str] | None,
        namespace: str,
        name: str,
    ) -> "RunPaths":
        return cls(
            root=ensure_state_root(state_dir),
            namespace=validate_identifier(namespace, label="namespace"),
            name=validate_identifier(name, label="name"),
        )

    @property
    def namespace_dir(self) -> Path:
        return self.root / self.namespace

    @property
    def run_dir(self) -> Path:
        return self.namespace_dir / self.name

    @property
    def meta(self) -> Path:
        return self.run_dir / "meta.json"

    @property
    def result(self) -> Path:
        return self.run_dir / "result.json"

    @property
    def log(self) -> Path:
        return self.run_dir / "terminal.log"

    @property
    def ready(self) -> Path:
        return self.run_dir / "runner.ready"

    @property
    def configured(self) -> Path:
        return self.run_dir / "tmux.configured"


def reserve_run(paths: RunPaths) -> None:
    """Atomically and permanently reserve one logical identity.

    The run directory itself is the reservation.  It is never removed implicitly, so
    failed starts retain evidence and cannot race a retry into overwriting it.
    """

    paths.namespace_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    if paths.namespace_dir.is_symlink():
        raise TmuxAgentError(f"namespace directory may not be a symlink: {paths.namespace_dir}")
    try:
        paths.run_dir.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise IdentityExistsError(
            f"run identity already exists: {paths.namespace}/{paths.name} ({paths.run_dir})"
        ) from exc


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def publish_json_once(path: Path, value: Mapping[str, Any]) -> bool:
    """Atomically publish one immutable terminal record without overwriting a peer."""

    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        try:
            os.link(temp_path, path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def read_json(path: Path, *, kind: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TmuxAgentError(f"{kind} is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise TmuxAgentError(f"{kind} is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise TmuxAgentError(f"{kind} has unsupported schema: {path}")
    return value


def iter_run_paths(root: Path, namespace: str | None = None) -> Iterator[RunPaths]:
    namespaces = [validate_identifier(namespace, label="namespace")] if namespace else []
    namespace_dirs = [root / namespaces[0]] if namespaces else sorted(root.iterdir())
    for namespace_dir in namespace_dirs:
        if not namespace_dir.is_dir() or namespace_dir.is_symlink():
            continue
        try:
            valid_namespace = validate_identifier(namespace_dir.name, label="namespace")
        except TmuxAgentError:
            continue
        for run_dir in sorted(namespace_dir.iterdir()):
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            try:
                valid_name = validate_identifier(run_dir.name, label="name")
            except TmuxAgentError:
                continue
            yield RunPaths(root=root, namespace=valid_namespace, name=valid_name)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TmuxAgentError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
