"""Managed Playbook session lifecycle composition.

The public CLI grows here in vertical slices. Provider-native identity remains
owned by provider hooks; this module only carries a short-lived launch
capability into a Playbook-owned tmux body.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import time
from typing import Callable, Mapping

from provider.session_identity import identity_spec, scrub_inherited_session_identity
from provider.sandbox import bypass_args, wrap_command_argv
from provider.session_state import (
    LaneTransitionConflict,
    ManagedLaunchReservation,
    ManagedLane,
    SessionKey,
    SessionStateError,
    _atomic_json,
    _launches_directory,
    _validate_launch,
    _validate_managed_name,
    _validate_record,
    assert_no_active_managed_resume,
    cancel_managed_launch,
    create_managed_lane,
    ensure_session_record,
    iter_session_directories,
    list_managed_lanes,
    record_lane_foreground,
    reserve_managed_launch,
    resolve_managed_lane,
    revoke_unclaimed_launches,
    session_directory,
    sessions_root_lock,
    transition_managed_lane,
)
from tasks.core import resolve_agent_dir
from tmux_agent import (
    TmuxClient,
    TmuxAgentError,
    UnexpectedForegroundError,
    attach_run,
    label_run,
    peek_run,
    resolve_agent_command,
    send_message,
    send_shell_command,
    shell_foreground_process_groups,
    start_run,
    status_run,
    stop_foreground_job,
    stop_run,
)


BOOTSTRAP_PROMPT = "Run `pb-tasks bootstrap`, then wait."
# The user's manual swap kept the outgoing lane's last 500 lines and the last
# 200 lines of the project chat log; a swap persists exactly that much.
HANDOFF_LANE_LINES = 500
HANDOFF_CHAT_LINES = 200
SESSION_TMUX_NAMESPACE = "sessions"
# A dispatch takes seconds (send_shell_command waits 5 s for the foreground);
# a generation still half-dispatched after this long lost its controller.
DISPATCH_ABANDONED_SECONDS = 60.0
TERMINAL_BODY_STATES = {"completed", "stopped", "failed", "lost"}
_PROVIDER_AGENTS = {
    "claude": "claude",
    "codex": "codex",
    "antigravity": "agy",
    "pi": "pi",
    "omp": "omp",
}

_STARTUP_ATTENTION_PATTERNS = (
    (
        "directory trust",
        "do you trust the contents of this directory?",
    ),
    (
        "hook review",
        "hooks need review",
    ),
)


def _startup_attention(pane_text: str) -> str | None:
    """Classify provider-owned security screens without answering them."""
    normalized = " ".join(pane_text.casefold().split())
    for label, marker in _STARTUP_ATTENTION_PATTERNS:
        if marker in normalized:
            return label
    return None


def _startup_identity_failure(pane_text: str) -> str | None:
    """Return a provider-visible identity failure that cannot self-heal."""

    markers = (
        "cannot bootstrap playbook session:",
        "managed launch environment is incomplete:",
        "managed launch capability is unknown or already consumed",
    )
    for line in reversed(pane_text.splitlines()):
        normalized = " ".join(line.casefold().split())
        if any(marker in normalized for marker in markers):
            return line.strip()
    return None


def _adapter(provider: str, project_root: Path):
    if provider == "claude":
        from provider.adapters.claude import ClaudeAdapter
        return ClaudeAdapter("", project_root)
    if provider == "codex":
        from provider.adapters.codex import CodexAdapter
        return CodexAdapter("", project_root)
    if provider == "antigravity":
        from provider.adapters.antigravity import AntigravityAdapter
        return AntigravityAdapter("", project_root)
    if provider == "pi":
        from provider.adapters.pi import PiAdapter
        return PiAdapter("", project_root)
    if provider == "omp":
        from provider.adapters.omp import OmpAdapter
        return OmpAdapter("", project_root)
    raise SessionStateError(f"unsupported interactive provider: {provider}")


def provider_support_error(provider: str, project_root: Path) -> str | None:
    conformance = _adapter(provider, project_root).session_conformance()
    if not conformance.supported:
        return conformance.unsupported_reason or "interactive session contract is unavailable"
    if not conformance.exact_resume:
        return "provider does not declare exact native resume"
    return None


def _config_path(agent_dir: Path) -> Path:
    return agent_dir / "sessions" / "config.json"


def _read_provider_config(path: Path) -> str:
    if path.is_symlink():
        raise SessionStateError("session config may not be a symlink")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionStateError(f"invalid session config: {path}") from exc
    if not isinstance(value, dict) or set(value) != {"schema", "default_provider"}:
        raise SessionStateError("session config fields are invalid")
    if value["schema"] != 1:
        raise SessionStateError("session config schema is unsupported")
    try:
        provider = identity_spec(value["default_provider"]).provider
    except (TypeError, ValueError) as exc:
        raise SessionStateError("session config default provider is invalid") from exc
    if provider != value["default_provider"]:
        raise SessionStateError("session config default provider must be canonical")
    return provider


def resolve_default_provider(agent_dir: Path, explicit: str | None = None) -> str:
    """Resolve and optionally remember the per-agent-dir interactive default."""

    if explicit is not None:
        try:
            provider = identity_spec(explicit).provider
        except (TypeError, ValueError) as exc:
            raise SessionStateError(f"unknown interactive provider: {explicit}") from exc
        with sessions_root_lock(agent_dir, create=True):
            path = _config_path(agent_dir)
            if path.is_symlink():
                raise SessionStateError("session config may not be a symlink")
            _atomic_json(path, {"schema": 1, "default_provider": provider})
        return provider
    path = _config_path(agent_dir)
    if not path.exists():
        if path.is_symlink():
            raise SessionStateError("session config may not be a symlink")
        return "codex"
    return _read_provider_config(path)


def resolve_start_provider(
    agent_dir: Path,
    project_root: Path,
    explicit: str | None = None,
) -> str:
    if explicit is None:
        provider = resolve_default_provider(agent_dir)
    else:
        try:
            provider = identity_spec(explicit).provider
        except (TypeError, ValueError) as exc:
            raise SessionStateError(f"unknown interactive provider: {explicit}") from exc
    unsupported = provider_support_error(provider, project_root)
    if unsupported:
        raise SessionStateError(f"managed {provider} session is unavailable: {unsupported}")
    if explicit is not None:
        resolve_default_provider(agent_dir, provider)
    return provider


def managed_launch_environment(
    reservation: ManagedLaunchReservation,
    project_root: Path,
    provider: str,
    inherited: Mapping[str, str],
) -> dict[str, str]:
    """Build a child environment with no parent conversation identity."""

    canonical = identity_spec(provider).provider
    if canonical != reservation.provider:
        raise ValueError("managed launch provider disagrees with its reservation")
    result = scrub_inherited_session_identity(inherited)
    # Hooks invoked inside the provider use the public ``pb-tasks`` command.
    # Keep that command on the same runtime as this session module so a source
    # checkout shim cannot accidentally launch against an older global install.
    runtime_bin = Path(__file__).resolve().parent.parent / "bin"
    runtime_tasks = runtime_bin / "pb-tasks"
    if not runtime_tasks.is_file():
        raise SessionStateError(
            f"managed session runtime is incomplete: missing {runtime_tasks}"
        )
    inherited_path = result.get("PATH", "")
    result["PATH"] = (
        f"{runtime_bin}{os.pathsep}{inherited_path}"
        if inherited_path
        else str(runtime_bin)
    )
    # A long-lived tmux server has an environment baseline of its own. Merely
    # omitting a parent identity from ``new-session -e`` lets that stale value
    # reappear in the new pane, so explicitly shadow every identity transport.
    # The launched provider/extension overwrites its own native transport for
    # hooks and commands after startup.
    result.update(
        {
            "PLAYBOOK_SESSION_ID": "",
            "PLAYBOOK_BRIDGE_PROVIDER": "",
            "PLAYBOOK_ROLE": "",
            "CLAUDE_CODE_SESSION_ID": "",
            "CODEX_THREAD_ID": "",
            "ANTIGRAVITY_CONVERSATION_ID": "",
        }
    )
    result.update(
        {
            "PLAYBOOK_PROVIDER": canonical,
            "PLAYBOOK_PROJECT_ROOT": str(project_root.resolve()),
            "PLAYBOOK_MANAGED_LAUNCH_TOKEN": reservation.token,
            "PLAYBOOK_MANAGED_BODY_ID": reservation.body_id,
            "PLAYBOOK_MANAGED_PROJECT_ROOT": str(project_root.resolve()),
            # Lets the lane say who it is (e.g. "lane-a"). The session record
            # stays the authority: a later rename is not reflected until the
            # next resume. Empty shadows a stale tmux-server baseline.
            "PLAYBOOK_SESSION_NAME": reservation.name or "",
        }
    )
    return result


def preflight_managed_launch_environment(
    environment: Mapping[str, str],
    *,
    provider: str,
    reservation: ManagedLaunchReservation,
) -> None:
    """Reject a malformed child boundary before creating a tmux body."""

    canonical = identity_spec(provider).provider
    expected = {
        "PLAYBOOK_PROVIDER": canonical,
        "PLAYBOOK_MANAGED_LAUNCH_TOKEN": reservation.token,
        "PLAYBOOK_MANAGED_BODY_ID": reservation.body_id,
    }
    disagreements = [
        f"{name}={environment.get(name)!r} (expected {value!r})"
        for name, value in expected.items()
        if environment.get(name) != value
    ]
    inherited = [
        name
        for name in (
            "PLAYBOOK_SESSION_ID",
            "PLAYBOOK_BRIDGE_PROVIDER",
            "CLAUDE_CODE_SESSION_ID",
            "CODEX_THREAD_ID",
            "ANTIGRAVITY_CONVERSATION_ID",
        )
        if environment.get(name)
    ]
    if disagreements or inherited:
        details = disagreements
        if inherited:
            details.append("unscrubbed identity transports: " + ", ".join(inherited))
        raise SessionStateError(
            "managed launch environment preflight failed: " + "; ".join(details)
        )


def managed_shell_environment(
    project_root: Path, inherited: Mapping[str, str]
) -> dict[str, str]:
    """Build a durable shell environment containing no provider capability."""

    result = scrub_inherited_session_identity(inherited)
    runtime_bin = Path(__file__).resolve().parent.parent / "bin"
    inherited_path = result.get("PATH")
    result["PATH"] = (
        f"{runtime_bin}{os.pathsep}{inherited_path}"
        if inherited_path
        else str(runtime_bin)
    )
    for key in (
        "PLAYBOOK_MANAGED_LAUNCH_TOKEN",
        "PLAYBOOK_MANAGED_BODY_ID",
        "PLAYBOOK_MANAGED_PROJECT_ROOT",
        "PLAYBOOK_PROVIDER",
        "PLAYBOOK_SESSION_ID",
        "PLAYBOOK_BRIDGE_PROVIDER",
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_THREAD_ID",
        "ANTIGRAVITY_CONVERSATION_ID",
    ):
        result[key] = ""
    result["PLAYBOOK_PROJECT_ROOT"] = str(project_root.resolve())
    return result


def create_managed_lane_body(
    *,
    agent_dir: Path,
    project: Path,
    cwd: Path,
    name: str | None,
    body_id: str | None = None,
    attach: bool = False,
) -> ManagedLane:
    """Create a durable shell lane and return after shell foreground readiness."""

    lane = create_managed_lane(
        agent_dir,
        project_root=project,
        name=name,
        cwd=cwd,
        body_id=body_id,
    )
    shell = Path(os.environ.get("SHELL", "/bin/zsh"))
    if not shell.is_absolute() or shell.name not in {"bash", "zsh"} or not shell.is_file():
        shell = Path("/bin/zsh" if Path("/bin/zsh").is_file() else "/bin/bash")
    shell_args = ["-f"] if shell.name == "zsh" else ["--noprofile", "--norc"]
    try:
        start_run(
            name=lane.record["body_id"],
            namespace=SESSION_TMUX_NAMESPACE,
            agent="command",
            agent_args=[str(shell), *shell_args],
            cwd=cwd,
            environment=managed_shell_environment(project, os.environ),
            model=None,
        )
        lane = transition_managed_lane(
            agent_dir,
            lane.record["body_id"],
            expected_state="provisional",
            state="shell_idle",
        )
        if name is not None:
            label_run(
                name=lane.record["body_id"],
                namespace=SESSION_TMUX_NAMESPACE,
                label=name,
            )
        if attach:
            result = attach_run(
                name=lane.record["body_id"], namespace=SESSION_TMUX_NAMESPACE
            )
            if result != 0:
                raise SessionStateError(f"tmux attach exited with status {result}")
        return lane
    except BaseException:
        try:
            stop_run(name=lane.record["body_id"], namespace=SESSION_TMUX_NAMESPACE)
        except TmuxAgentError:
            pass
        try:
            transition_managed_lane(
                agent_dir,
                lane.record["body_id"],
                expected_state=lane.record["state"],
                state="destroyed",
            )
        except SessionStateError:
            pass
        raise


def dispatch_provider_generation(
    *,
    agent_dir: Path,
    lane_address: str,
    provider: str,
    model: str | None = None,
    sandbox: bool = False,
    resume_session_id: str | None = None,
    prompt: str | None = None,
) -> tuple[ManagedLane, ManagedLaunchReservation]:
    """Reserve and dispatch one provider generation into an idle shell lane."""

    lane = resolve_managed_lane(agent_dir, lane_address)
    if lane.record["state"] not in {"shell_idle", "provider_exited"}:
        raise SessionStateError(
            f"managed lane {lane_address} is not idle: {lane.record['state']}"
        )
    # A resumed record keeps its own name (the bind refuses a silent rename);
    # a new conversation carries none, since its lane holds the name.
    record_name = None
    if resume_session_id is not None:
        _path, existing = resolve_session_record(
            agent_dir, f"{provider}:{resume_session_id}"
        )
        record_name = existing.get("name")
    lane = transition_managed_lane(
        agent_dir,
        lane.record["body_id"],
        expected_state=lane.record["state"],
        state="launch_reserved" if lane.record["generation"] == 0 else "relaunching",
        launch_provider=provider,
        advance_generation=True,
    )
    reservation: ManagedLaunchReservation | None = None
    script_path: Path | None = None
    dispatch_attempted = False
    try:
        reservation = reserve_managed_launch(
            agent_dir,
            project_root=Path(lane.record["project"]),
            provider=provider,
            name=record_name,
            cwd=Path(lane.record["cwd"]),
            body_id=lane.record["body_id"],
            model=model,
            sandbox=sandbox,
            operation="resume" if resume_session_id is not None else "new",
            expected_key=(
                SessionKey.from_values(provider, resume_session_id)
                if resume_session_id is not None
                else None
            ),
        )
        adapter = _adapter(provider, Path(lane.record["project"]))
        argv = resolve_agent_command(
            _PROVIDER_AGENTS[provider],
            adapter.interactive_argv(
                prompt=BOOTSTRAP_PROMPT if prompt is None else prompt,
                model=model,
                resume_session_id=resume_session_id,
            ),
        )
        if sandbox:
            permission_args = (
                bypass_args(argv[0]) if Path(argv[0]).name in {"claude", "codex", "agy", "pi"} else []
            )
            argv = wrap_command_argv([argv[0], *permission_args, *argv[1:]], Path(lane.record["project"]))
        generation_environment = managed_launch_environment(
            reservation,
            Path(lane.record["project"]),
            provider,
            {},
        )
        preflight_managed_launch_environment(
            generation_environment, provider=provider, reservation=reservation
        )
        # PATH is the shell's own (runtime bin first, then the caller's
        # PATH); the generation environment is built without a parent, so
        # its PATH would hold only the runtime bin.
        injected = {
            key: generation_environment[key]
            for key in (
                "PLAYBOOK_PROVIDER",
                "PLAYBOOK_PROJECT_ROOT",
                "PLAYBOOK_MANAGED_LAUNCH_TOKEN",
                "PLAYBOOK_MANAGED_BODY_ID",
                "PLAYBOOK_MANAGED_PROJECT_ROOT",
                "PLAYBOOK_SESSION_NAME",
            )
        }
        # The agent learns which lane it is in ("you are lane-a").
        injected["PLAYBOOK_SESSION_NAME"] = lane.record["name"] or record_name or ""
        if sandbox:
            injected["PLAYBOOK_SANDBOXED"] = "1"
        command = [
            "env",
            "-u", "PLAYBOOK_SESSION_ID",
            "-u", "PLAYBOOK_BRIDGE_PROVIDER",
            "-u", "CLAUDE_CODE_SESSION_ID",
            "-u", "CODEX_THREAD_ID",
            "-u", "ANTIGRAVITY_CONVERSATION_ID",
            *(f"{key}={value}" for key, value in injected.items()),
            *argv,
        ]
        script_path = _launcher_path(lane)
        commands_dir = script_path.parent
        if commands_dir.is_symlink():
            raise SessionStateError("managed lane commands directory may not be a symlink")
        commands_dir.mkdir(mode=0o700, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{lane.record['body_id']}-g{lane.record['generation']}.",
            suffix=".tmp",
            dir=commands_dir,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write("#!/bin/sh\n")
                stream.write(f"rm -f -- {shlex.quote(str(script_path))}\n")
                stream.write(f"exec {shlex.join(command)}\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, script_path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        dispatch_attempted = True
        foreground = send_shell_command(
            name=lane.record["body_id"],
            namespace=SESSION_TMUX_NAMESPACE,
            command=shlex.join(["/bin/sh", str(script_path)]),
        )
        # The provider's hook may bind before this lands; that keeps its identity.
        lane = record_lane_foreground(
            agent_dir,
            lane.record["body_id"],
            generation=lane.record["generation"],
            foreground_process_group=foreground,
        )
        return lane, reservation
    except BaseException as exc:
        # A busy shell is refused before anything is typed.
        typed = dispatch_attempted and not isinstance(exc, UnexpectedForegroundError)
        if typed:
            try:
                recover_lane_dispatch(agent_dir, lane)
            except (SessionStateError, TmuxAgentError, OSError):
                pass  # `settle_lane` retries once the dispatch is stale.
            raise
        if script_path is not None:
            script_path.unlink(missing_ok=True)
        if reservation is not None:
            cancel_managed_launch(agent_dir, reservation)
        try:
            transition_managed_lane(
                agent_dir,
                lane.record["body_id"],
                expected_state=lane.record["state"],
                state="shell_idle",
            )
        except SessionStateError:
            pass
        raise


def _dispatch_abandoned(record: Mapping[str, object]) -> bool:
    """Whether a generation stayed half-dispatched far longer than a dispatch takes."""

    starting = record["state"] in {"launch_reserved", "relaunching"} or (
        record["state"] == "provider_running" and record["foreground_process_group"] is None
    )
    if not starting:
        return False
    since = datetime.fromisoformat(str(record["updated_at"]).replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - since).total_seconds() > DISPATCH_ABANDONED_SECONDS


def _launcher_path(lane: ManagedLane) -> Path:
    record = lane.record
    return lane.path.parent / ".commands" / f"{record['body_id']}-g{record['generation']}.sh"


def recover_lane_dispatch(agent_dir: Path, lane: ManagedLane) -> ManagedLane:
    """Settle a generation whose launcher may or may not have been typed.

    The launcher deletes itself before anything else, so removing it here
    decides: if it was still there, it never ran and now never can. If it
    was gone, it ran, and the shell's foreground says whether the agent
    still runs.
    """

    body_id = str(lane.record["body_id"])
    generation = lane.record["generation"]
    try:
        _launcher_path(lane).unlink()
        ran = False
    except FileNotFoundError:
        ran = True
    if ran:
        shell_group, foreground = shell_foreground_process_groups(
            name=body_id, namespace=SESSION_TMUX_NAMESPACE
        )
        if foreground != shell_group:
            return record_lane_foreground(
                agent_dir, body_id, generation=generation, foreground_process_group=foreground
            )
    # No process of this generation remains to present its token.
    revoke_unclaimed_launches(agent_dir, body_id)
    current = resolve_managed_lane(agent_dir, body_id)
    if current.record["generation"] != generation or current.record["state"] not in {
        "launch_reserved",
        "relaunching",
        "provider_running",
    }:
        return current
    if not ran:
        return transition_managed_lane(
            agent_dir,
            body_id,
            expected_state=current.record["state"],
            expected_generation=generation,
            state="shell_idle",
        )
    current = _lane_exited(agent_dir, current)
    _mark_lane_session_stopped(agent_dir, current, "natural")
    return current


def list_session_records(
    agent_dir: Path,
    *,
    include_destroyed: bool = False,
) -> list[tuple[Path, dict]]:
    """Return recognized native records in stable human-address order."""

    records: list[tuple[Path, dict]] = []
    for _key, directory in iter_session_directories(agent_dir):
        path = directory / "session.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("state") == "destroyed" and not include_destroyed:
            continue
        records.append((path, record))
    records.sort(
        key=lambda item: (
            item[1].get("name") is None,
            (item[1].get("name") or "").casefold(),
            item[1]["provider"],
            item[1]["session_id"],
        )
    )
    return records


def resolve_session_record(
    agent_dir: Path,
    address: str,
    *,
    include_destroyed: bool = False,
) -> tuple[Path, dict]:
    """Resolve one exact human name or provider-qualified native key."""

    if not address:
        raise SessionStateError("session address may not be empty")
    matches: list[tuple[Path, dict]] = []
    if ":" in address:
        provider, separator, native_id = address.partition(":")
        if not separator:
            raise SessionStateError("session native address must be provider:native-id")
        try:
            key = SessionKey.from_values(provider, native_id)
        except (TypeError, ValueError) as exc:
            raise SessionStateError(f"invalid session address: {address}") from exc
        directory = session_directory(agent_dir, key)
        path = directory / "session.json"
        if path.is_file() and not path.is_symlink():
            try:
                record = _validate_record(
                    json.loads(path.read_text(encoding="utf-8")), key
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise SessionStateError(f"invalid session record: {path}") from exc
            if include_destroyed or record.get("state") != "destroyed":
                matches.append((path, record))
    else:
        matches = [
            item
            for item in list_session_records(
                agent_dir, include_destroyed=include_destroyed
            )
            if item[1].get("name") == address
        ]
    if not matches:
        raise SessionStateError(f"session not found: {address}")
    if len(matches) != 1:
        identities = ", ".join(
            f"{record['provider']}:{record['session_id']}" for _, record in matches
        )
        raise SessionStateError(
            f"session name matches multiple native records: {address} ({identities})"
        )
    return matches[0]


def _session_task_authorities(
    agent_dir: Path, key: SessionKey
) -> tuple[list[tuple[str, Path]], list[str]]:
    """Find task.md authorities for one session without trusting its cache."""
    from tasks.task_document import TaskDocument, TaskDocumentError

    tasks = agent_dir / "tasks"
    if tasks.is_symlink():
        return [], ["task authority root is a symlink; current task is unavailable"]
    if not tasks.is_dir():
        return [], []
    claims: list[tuple[str, Path]] = []
    diagnostics: list[str] = []
    owner_line = re.compile(
        rf"^-\s+{re.escape(key.provider)}:{re.escape(key.session_id)}\s*$",
        re.MULTILINE,
    )
    for child in sorted(tasks.iterdir(), key=lambda path: path.name):
        prefix, separator, _name = child.name.partition("-")
        if not separator or not prefix.isdigit() or child.is_symlink() or not child.is_dir():
            continue
        task_file = child / "task.md"
        if task_file.is_symlink() or not task_file.is_file():
            continue
        try:
            text = task_file.read_text(encoding="utf-8")
        except OSError as exc:
            diagnostics.append(f"task authority is unreadable at {task_file}: {exc}")
            continue
        mentions_session = owner_line.search(text) is not None
        try:
            document = TaskDocument.parse(text)
            owner = document.live_owner
        except TaskDocumentError as exc:
            if mentions_session:
                diagnostics.append(
                    f"malformed candidate task authority at {task_file}: {exc}"
                )
            continue
        if owner == key:
            claims.append((str(int(prefix)).zfill(3), task_file))
    return claims, diagnostics


def native_resume_route(
    record: Mapping[str, object] | None,
    provider: str,
    session_id: str,
    *,
    fallback_cwd: Path | None = None,
    prompt: str | None = None,
    attach: bool = False,
) -> tuple[str, list[str]] | None:
    """Return the cwd and argv that resume one exact native conversation.

    Managed sessions go back into a tmux body through ``pb-session resume``;
    any other session resumes in the caller's terminal with the provider's own
    command. The recorded ``resume_cwd`` wins over ``fallback_cwd``; without
    either there is no honest route and the result is ``None``.
    """
    record = record or {}
    recorded_cwd = record.get("resume_cwd")
    if isinstance(recorded_cwd, str) and recorded_cwd:
        cwd = recorded_cwd
    elif fallback_cwd is not None:
        cwd = str(fallback_cwd)
    else:
        return None
    if record.get("managed") is True:
        argv = ["pb-session", "resume", f"{provider}:{session_id}"]
        if attach:
            argv.append("--attach")
        if prompt is not None:
            argv.extend(["--prompt", prompt])
        return cwd, argv
    adapter = _adapter(provider, Path(record.get("project") or cwd))
    argv = resolve_agent_command(
        _PROVIDER_AGENTS[provider],
        adapter.interactive_argv(
            prompt=BOOTSTRAP_PROMPT if prompt is None else prompt,
            resume_session_id=session_id,
        ),
    )
    return cwd, argv


def _native_status(
    agent_dir: Path,
    address: str,
    *,
    include_destroyed: bool = False,
    observe_body: bool = True,
) -> dict:
    path, record = resolve_session_record(
        agent_dir, address, include_destroyed=include_destroyed
    )
    body_id = record.get("body_id")
    body_state = "none" if body_id is None else "unobserved"
    body_exit = None
    body_error = None
    if observe_body and body_id is not None:
        try:
            observed = status_run(name=body_id, namespace=SESSION_TMUX_NAMESPACE)
            body_state = observed.get("state", "unknown")
            body_exit = observed.get("exit_status")
        except TmuxAgentError as exc:
            body_state = "unavailable"
            body_error = str(exc)
    state_file = path.parent / "current_state"
    task = state_file.read_text(encoding="utf-8").strip() if state_file.is_file() else None
    diagnostics: list[str] = []
    cached_task = None
    from tasks.task_document import (
        TaskDocument,
        TaskDocumentError,
        validate_task_claim,
    )
    if task:
        key = SessionKey.from_values(record["provider"], record["session_id"])
        try:
            validate_task_claim(agent_dir, key, task)
            cached_task = task
        except (OSError, TaskDocumentError) as exc:
            diagnostics.append(
                f"navigation cache is stale or disagrees with task authority: {exc}; "
                f"repair with pb-tasks work {task} after resolving ownership"
            )
    key = SessionKey.from_values(record["provider"], record["session_id"])
    authoritative_tasks, authority_diagnostics = _session_task_authorities(
        agent_dir, key
    )
    diagnostics.extend(authority_diagnostics)
    current_task = None
    current_gate = None
    if len(authoritative_tasks) == 1:
        current_task, authoritative_task_file = authoritative_tasks[0]
        try:
            authoritative_document = TaskDocument.parse(
                authoritative_task_file.read_text(encoding="utf-8")
            )
            completed, total = authoritative_document.progress
            open_gate = next(
                (gate for gate in authoritative_document.gates if not gate.checked),
                None,
            )
            current_gate = {
                "line": open_gate.line + 1 if open_gate is not None else None,
                "text": authoritative_document.head_position,
                "completed": completed,
                "total": total,
            }
        except (OSError, TaskDocumentError) as exc:
            diagnostics.append(f"current task gate is unreadable: {exc}")
        if cached_task is not None and cached_task != current_task:
            diagnostics.append(
                f"navigation cache identifies task {cached_task}, but task authority "
                f"identifies task {current_task}"
            )
    elif len(authoritative_tasks) > 1:
        rendered = ", ".join(
            f"{number} ({path})" for number, path in authoritative_tasks
        )
        diagnostics.append(
            f"multiple task authorities claim this session: {rendered}; "
            "resolve ownership before resuming governed work"
        )
    latest_activity = None
    latest_task = None
    chat_log = agent_dir / "chat_log.md"
    if chat_log.is_file() and not chat_log.is_symlink():
        from tasks.chat_state import parse_chat_entries

        try:
            entries = parse_chat_entries(chat_log.read_text(encoding="utf-8"))
        except OSError as exc:
            diagnostics.append(f"sparse chronology is unreadable: {exc}")
        else:
            for entry in entries:
                if (
                    entry.provider != record["provider"]
                    or entry.session_id != record["session_id"]
                ):
                    continue
                latest_activity = {
                    "marker": entry.marker,
                    "timestamp": entry.timestamp,
                    "body": entry.body,
                }
                import re

                matched = re.fullmatch(r"(?:T|G)(\d+)(?::[^\s]+)", entry.marker)
                if matched:
                    latest_task = matched.group(1).zfill(3)

    lifecycle = record.get("state", "ad_hoc")
    lifecycle_at_field = {
        "running": "last_started_at",
        "stopped": "stopped_at",
        "destroyed": "destroyed_at",
        "ad_hoc": "created_at",
    }[lifecycle]
    recorded_lifecycle = {
        "state": lifecycle,
        "at": record.get(lifecycle_at_field),
    }
    observed_body = {
        "state": body_state,
        "exit_status": body_exit,
        "error": body_error,
    }
    if body_error:
        diagnostics.append(
            f"tmux body observation failed for {body_id}: {body_error}; "
            "the durable native identity remains resumable"
        )

    resume_command = None
    if lifecycle != "destroyed":
        route = native_resume_route(record, record["provider"], record["session_id"])
        if route is None:
            diagnostics.append(
                "manual resume route is incomplete: session has no recorded resume_cwd"
            )
        else:
            import shlex

            resume_cwd, resume_argv = route
            resume_command = (
                f"cd {shlex.quote(resume_cwd)} && {shlex.join(resume_argv)}"
            )
    return {
        "address": record.get("name") or f"{record['provider']}:{record['session_id']}",
        "provider": record["provider"],
        "session_id": record["session_id"],
        "name": record.get("name"),
        "managed": record.get("managed", False),
        "lifecycle_state": record.get("state", "ad_hoc"),
        "body_id": body_id,
        "body_state": body_state,
        "body_exit_status": body_exit,
        "body_error": body_error,
        "task": task,
        "recorded_lifecycle": recorded_lifecycle,
        "observed_body": observed_body,
        "current_task": current_task,
        "current_gate": current_gate,
        "latest_recorded_task": latest_task,
        "latest_recorded_activity": latest_activity,
        "chronology_complete": False,
        "chronology_note": (
            "best-effort sparse chronology: successful append-written events may be "
            "absent after a reported cross-file publication failure"
        ),
        "resume_command": resume_command,
        "diagnostics": diagnostics,
        "sandbox": record.get("sandbox", False),
        "tmux_session": record.get("tmux_session"),
        "tmux_pane": record.get("tmux_pane"),
        "resume_cwd": record.get("resume_cwd"),
    }


_LANE_LIFECYCLE = {
    "provisional": "starting",
    "shell_idle": "shell",
    "launch_reserved": "starting",
    "relaunching": "starting",
    "provider_running": "starting",
    "provider_exited": "shell",
    "destroyed": "destroyed",
}


def _lane_summary(
    lane: ManagedLane, *, current: bool, observe_body: bool
) -> dict[str, object]:
    record = lane.record
    attention = None
    if observe_body and record["state"] == "provider_running" and record["session_id"] is None:
        # An agent that has not bound yet may be waiting on a screen only a
        # person can answer, or may have failed in a way that cannot heal.
        try:
            pane = peek_run(
                name=str(record["body_id"]), namespace=SESSION_TMUX_NAMESPACE, lines=80
            )
        except (TmuxAgentError, OSError):
            pane = ""
        attention = _startup_identity_failure(pane) or _startup_attention(pane)
    return {
        "name": record["name"],
        "body_id": record["body_id"],
        "state": record["state"],
        "generation": record["generation"],
        "agent": record["launch_provider"],
        "bound": record["session_id"] is not None,
        "current": current,
        "attention": attention,
    }


def _unbound_lane_status(lane: ManagedLane, *, observe_body: bool) -> dict:
    record = lane.record
    body_state, body_error = "unobserved", None
    if observe_body:
        try:
            body_state = status_run(
                name=str(record["body_id"]), namespace=SESSION_TMUX_NAMESPACE
            ).get("state", "unknown")
        except TmuxAgentError as exc:
            body_state, body_error = "unavailable", str(exc)
    return {
        "address": _lane_label(record),
        "provider": record["launch_provider"],
        "session_id": None,
        "name": record["name"],
        "managed": True,
        "lifecycle_state": _LANE_LIFECYCLE[record["state"]],
        "body_id": record["body_id"],
        "body_state": body_state,
        "body_exit_status": None,
        "body_error": body_error,
        "task": None,
        "recorded_lifecycle": {
            "state": _LANE_LIFECYCLE[record["state"]],
            "at": record["updated_at"],
        },
        "observed_body": {"state": body_state, "exit_status": None, "error": body_error},
        "current_task": None,
        "current_gate": None,
        "latest_recorded_task": None,
        "latest_recorded_activity": None,
        "chronology_complete": False,
        "chronology_note": "no conversation has bound in this lane yet",
        "resume_command": None,
        "diagnostics": [],
        "sandbox": False,
        "tmux_session": None,
        "tmux_pane": None,
        "resume_cwd": record["cwd"],
    }


def session_status(
    agent_dir: Path,
    address: str,
    *,
    include_destroyed: bool = False,
    observe_body: bool = True,
) -> dict:
    """Orientation for one lane or one conversation.

    A lane name answers with the conversation it runs or last ran, plus a
    `lane` block; a native key answers for that conversation, with the lane
    it last ran in.
    """

    lane = _lane_or_none(agent_dir, address, include_destroyed=include_destroyed)
    if lane is not None:
        if observe_body:
            lane = settle_lane(agent_dir, lane)
        key = _lane_key(lane.record)
        value = None
        if key is not None:
            try:
                value = _native_status(
                    agent_dir,
                    f"{key.provider}:{key.session_id}",
                    include_destroyed=True,
                    observe_body=observe_body,
                )
            except SessionStateError:
                value = None
        if value is None:
            value = _unbound_lane_status(lane, observe_body=observe_body)
        value["address"] = _lane_label(lane.record)
        value["lane"] = _lane_summary(lane, current=True, observe_body=observe_body)
        return value
    value = _native_status(
        agent_dir, address, include_destroyed=include_destroyed, observe_body=observe_body
    )
    _path, record = resolve_session_record(agent_dir, address, include_destroyed=True)
    owner = _lane_of_record(agent_dir, record)
    if owner is not None:
        key = SessionKey.from_values(record["provider"], record["session_id"])
        current = owner.record["state"] != "destroyed" and _lane_key(owner.record) == key
        if current:
            value["address"] = _lane_label(owner.record)
        if not (current and owner.record["state"] == "provider_running"):
            # The lane's shell may be alive, but not as this conversation.
            value["body_state"] = "none"
            value["body_exit_status"] = None
            value["observed_body"] = {"state": "none", "exit_status": None, "error": None}
        value["lane"] = _lane_summary(owner, current=current, observe_body=False)
    return value


def list_session_status(agent_dir: Path, *, include_destroyed: bool = False) -> list[dict]:
    """Every conversation record, then every live lane that has none."""

    lanes = [settle_lane(agent_dir, lane) for lane in list_managed_lanes(agent_dir)]
    values = [
        session_status(
            agent_dir,
            f"{record['provider']}:{record['session_id']}",
            include_destroyed=include_destroyed,
        )
        for _path, record in list_session_records(
            agent_dir, include_destroyed=include_destroyed
        )
    ]
    listed = {(value["provider"], value["session_id"]) for value in values}
    for lane in lanes:
        if lane.record["state"] == "destroyed":
            continue
        key = _lane_key(lane.record)
        if key is not None and (key.provider, key.session_id) in listed:
            continue
        values.append(session_status(agent_dir, str(lane.record["body_id"])))
    return values


def _managed_body(
    agent_dir: Path, address: str
) -> tuple[ManagedLane | None, str]:
    """The tmux body an address names, with its lane when it has one.

    A conversation's key names its lane only while that lane still holds it;
    once the lane has moved on to another agent the key no longer reaches it.
    """

    lane = _lane_or_none(agent_dir, address)
    if lane is not None:
        return lane, str(lane.record["body_id"])
    _path, record = resolve_session_record(agent_dir, address)
    body_id = record.get("body_id")
    if record.get("managed") is not True or not body_id:
        raise SessionStateError(f"session has no managed tmux body: {address}")
    lane = _lane_of_record(agent_dir, record)
    if lane is None:
        return None, body_id  # launched before lanes: the body is the agent
    key = SessionKey.from_values(record["provider"], record["session_id"])
    label = _lane_label(lane.record)
    if lane.record["state"] == "destroyed":
        raise SessionStateError(
            f"lane {label} is closed; `pb-session resume {address}` reopens the conversation"
        )
    holder = _lane_key(lane.record)
    if holder != key:
        raise SessionStateError(
            f"lane {label} has moved on to "
            f"{f'{holder.provider}:{holder.session_id}' if holder else 'another agent'}; "
            f"address the lane as `{label}`, or `pb-session resume {address}`"
        )
    return lane, body_id


def attach_session(agent_dir: Path, address: str) -> int:
    _lane, body_id = _managed_body(agent_dir, address)
    return attach_run(name=body_id, namespace=SESSION_TMUX_NAMESPACE)


def peek_session(agent_dir: Path, address: str, *, lines: int = 50) -> str:
    _lane, body_id = _managed_body(agent_dir, address)
    return peek_run(name=body_id, namespace=SESSION_TMUX_NAMESPACE, lines=lines)


def send_session(agent_dir: Path, address: str, message: str, *, enter: bool = True) -> None:
    lane, body_id = _managed_body(agent_dir, address)
    expected_foreground = None
    if lane is not None:
        lane = settle_lane(agent_dir, lane)
        label = _lane_label(lane.record)
        expected_foreground = lane.record["foreground_process_group"]
        if lane.record["state"] != "provider_running" or expected_foreground is None:
            # With no agent in the foreground the text would reach the shell
            # and run as a command.
            raise SessionStateError(
                f"lane {label} has no agent running ({lane.record['state']}); "
                f"`pb-session attach {label}` to use its shell"
            )
    try:
        send_message(
            name=body_id,
            namespace=SESSION_TMUX_NAMESPACE,
            message=message,
            enter=enter,
            expected_foreground=expected_foreground,
        )
    except UnexpectedForegroundError:
        # The agent exited, or a swap replaced it, after the lane was read.
        raise SessionStateError(
            f"lane {label} changed its foreground job before the send; nothing was "
            f"sent. Check `pb-session status {label}` and send again"
        ) from None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _chronology_timestamp(value: object) -> str:
    """Render a persisted lifecycle timestamp in the chat-log grammar."""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except ValueError:
            pass
    from tasks.chat_state import chat_timestamp
    return chat_timestamp()


def _append_session_chronology(
    agent_dir: Path,
    record: Mapping[str, object],
    kind: str,
    message: str,
    *event_parts: object,
    timestamp_field: str | None = None,
) -> bool:
    """Best-effort projection after one authoritative session transition."""
    from tasks.chat_state import append_chat_event, derive_event_key

    provider = str(record["provider"])
    session_id = str(record["session_id"])
    marker = f"S:{kind}"
    timestamp = _chronology_timestamp(
        record.get(timestamp_field) if timestamp_field else None
    )
    try:
        append_chat_event(
            agent_dir,
            marker,
            provider,
            session_id,
            timestamp,
            message,
            event_key=derive_event_key(
                marker, provider, session_id, *event_parts
            ),
        )
    except (OSError, ValueError) as exc:
        print(
            "Warning: authoritative session state committed but chat chronology "
            f"could not be appended: {exc}",
            file=sys.stderr,
        )
        return False
    return True


def reconcile_lane_binding(agent_dir: Path, lane_address: str) -> dict | None:
    """Idempotently project a hook-finalized lane bind into session metadata/log."""

    lane = resolve_managed_lane(agent_dir, lane_address).record
    provider = lane.get("provider")
    session_id = lane.get("session_id")
    if not isinstance(provider, str) or not isinstance(session_id, str):
        return None
    body = status_run(
        name=str(lane["body_id"]), namespace=SESSION_TMUX_NAMESPACE
    )
    enrich: dict[str, object] = {}
    if isinstance(body.get("tmux_session"), str):
        enrich["tmux_session"] = body["tmux_session"]
    if isinstance(body.get("pane_id"), str):
        enrich["tmux_pane"] = body["pane_id"]
    _path, record = ensure_session_record(
        agent_dir, provider, session_id, enrich=enrich, environment={}
    )
    lane_name = lane.get("name") or lane["body_id"]
    _append_session_chronology(
        agent_dir,
        record,
        "bind",
        f"bound lane {lane_name} generation {lane['generation']} to native session",
        lane["body_id"],
        lane["generation"],
        timestamp_field="last_started_at",
    )
    # A new launch creates its record as it binds; a record that already
    # existed before this generation bound is a conversation being resumed.
    resumed = isinstance(lane.get("last_bound_at"), str) and str(
        record.get("created_at", "")
    ) < lane["last_bound_at"]
    _append_session_chronology(
        agent_dir,
        record,
        "resume" if resumed else "start",
        f"{'resumed' if resumed else 'started'} provider generation "
        f"{lane['generation']} in lane {lane_name}",
        lane["body_id"],
        lane["generation"],
        timestamp_field="last_started_at",
    )
    return record


def reconcile_lane_runtime(agent_dir: Path, lane_address: str) -> ManagedLane:
    """Converge a running generation after its foreground provider exits."""

    lane = resolve_managed_lane(agent_dir, lane_address)
    if lane.record["state"] != "provider_running":
        return lane
    shell_group, foreground = shell_foreground_process_groups(
        name=str(lane.record["body_id"]), namespace=SESSION_TMUX_NAMESPACE
    )
    if foreground != shell_group:
        return lane
    return _lane_exited(agent_dir, lane)


def _lane_exited(agent_dir: Path, lane: ManagedLane, *, state: str = "provider_exited") -> ManagedLane:
    # Keep the last bound conversation: `resume <lane>` and `swap` read it.
    record = lane.record
    return transition_managed_lane(
        agent_dir,
        str(record["body_id"]),
        expected_state=str(record["state"]),
        expected_generation=record["generation"],
        state=state,
        provider=record["provider"],
        session_id=record["session_id"],
        launch_provider=record["launch_provider"],
        last_bound_at=record["last_bound_at"],
    )


def _lane_key(record: Mapping[str, object]) -> SessionKey | None:
    if record.get("provider") is None:
        return None
    return SessionKey.from_values(record["provider"], record["session_id"])


def _lane_label(record: Mapping[str, object]) -> str:
    return str(record.get("name") or record["body_id"])


def _mark_lane_session_stopped(agent_dir: Path, lane: ManagedLane, how: str) -> None:
    """Record that the lane's bound conversation is no longer running there."""

    key = _lane_key(lane.record)
    if key is None:
        return
    try:
        _path, record = resolve_session_record(agent_dir, f"{key.provider}:{key.session_id}")
    except SessionStateError:
        return
    body_id = lane.record["body_id"]
    if record.get("state") != "running" or record.get("body_id") != body_id:
        return
    _path, stopped = ensure_session_record(
        agent_dir,
        key.provider,
        key.session_id,
        enrich={"state": "stopped", "stopped_at": _now()},
        expected={"state": "running", "body_id": body_id},
        environment={},
    )
    verb = "stopped" if how == "explicit" else "ended"
    _append_session_chronology(
        agent_dir,
        stopped,
        "stop",
        f"{verb} generation {lane.record['generation']} in lane {_lane_label(lane.record)}",
        body_id,
        lane.record["generation"],
        how,
        timestamp_field="stopped_at",
    )


def settle_lane(agent_dir: Path, lane: ManagedLane) -> ManagedLane:
    """Bring a lane record in line with what its tmux body shows now.

    A body that ended (after a reboot, or a killed tmux server) destroys the
    lane but keeps its last conversation; a provider that returned control
    to the shell makes the lane idle. Unobservable bodies change nothing.
    """

    try:
        return _settle_lane_once(agent_dir, lane)
    except LaneTransitionConflict:
        # Another controller settled or relaunched it between our look and write.
        return resolve_managed_lane(agent_dir, str(lane.record["body_id"]))


def _settle_lane_once(agent_dir: Path, lane: ManagedLane) -> ManagedLane:
    if lane.record["state"] == "destroyed":
        return lane
    body_id = str(lane.record["body_id"])
    try:
        observed = status_run(name=body_id, namespace=SESSION_TMUX_NAMESPACE)
    except TmuxAgentError:
        return lane
    if observed.get("state") in TERMINAL_BODY_STATES:
        lane = _lane_exited(agent_dir, lane, state="destroyed")
        _mark_lane_session_stopped(agent_dir, lane, "natural")
        return lane
    if _dispatch_abandoned(lane.record):
        # Its controller died mid-dispatch (a closed terminal, a kill).
        try:
            return recover_lane_dispatch(agent_dir, lane)
        except TmuxAgentError:
            return lane
    if lane.record["state"] == "provider_running":
        try:
            settled = reconcile_lane_runtime(agent_dir, body_id)
            if settled.record["session_id"] is not None:
                # The provider's hook bound it; record the bind and start
                # once (event keys make repeats no-ops).
                reconcile_lane_binding(agent_dir, body_id)
        except TmuxAgentError:
            return lane
        if settled.record["state"] == "provider_exited":
            _mark_lane_session_stopped(agent_dir, settled, "natural")
        return settled
    return lane


def stop_lane_provider(agent_dir: Path, lane: ManagedLane) -> ManagedLane:
    """Stop only the lane's admitted agent; its shell stays open."""

    record = lane.record
    if record["state"] != "provider_running" or record["foreground_process_group"] is None:
        raise SessionStateError(
            f"lane {_lane_label(record)} has no running agent (state {record['state']})"
        )
    stop_foreground_job(
        name=str(record["body_id"]),
        namespace=SESSION_TMUX_NAMESPACE,
        expected_process_group=int(record["foreground_process_group"]),
    )
    lane = _lane_exited(agent_dir, lane)
    _mark_lane_session_stopped(agent_dir, lane, "explicit")
    return lane


@dataclass(frozen=True)
class SwapHandoff:
    """An inspectable snapshot written before a lane changes agents."""

    path: Path
    lane: str
    outgoing: SessionKey | None
    task: str | None


def _tail_lines(text: str, count: int) -> list[str]:
    # tmux pads the visible screen with blank rows; they are not history.
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines[-count:] if count > 0 else []


def _fenced(lines: list[str]) -> str:
    longest = max(
        (len(run) for line in lines for run in re.findall(r"`+", line)), default=0
    )
    fence = "`" * max(3, longest + 1)
    return "\n".join([fence, *lines, fence])


def capture_swap_handoff(
    agent_dir: Path,
    lane_address: str,
    *,
    lane_lines: int = HANDOFF_LANE_LINES,
    chat_lines: int = HANDOFF_CHAT_LINES,
) -> SwapHandoff:
    """Persist the lane's last screen lines, chat tail and owned task.

    The capture reads only this lane's own tmux body. It refuses before
    writing anything when the outgoing agent's task cannot be named exactly,
    because the replacement would otherwise claim the wrong task or none.
    """

    lane = resolve_managed_lane(agent_dir, lane_address).record
    label = lane["name"] or lane["body_id"]
    outgoing = None
    task = None
    if lane["provider"] is not None:
        outgoing = SessionKey.from_values(lane["provider"], lane["session_id"])
        claims, diagnostics = _session_task_authorities(agent_dir, outgoing)
        if len(claims) > 1:
            numbers = ", ".join(number for number, _path in claims)
            raise SessionStateError(
                f"{outgoing.provider}:{outgoing.session_id} owns several tasks ({numbers}); "
                "close or hand off all but one before swapping"
            )
        if diagnostics:
            raise SessionStateError(
                "cannot tell which task the outgoing agent owns: " + "; ".join(diagnostics)
            )
        task = claims[0][0] if claims else None
    screen = _tail_lines(
        peek_run(
            name=str(lane["body_id"]),
            namespace=SESSION_TMUX_NAMESPACE,
            lines=lane_lines,
        ),
        lane_lines,
    )
    chat_path = agent_dir / "chat_log.md"
    chat = (
        _tail_lines(chat_path.read_text(encoding="utf-8"), chat_lines)
        if chat_path.is_file() and not chat_path.is_symlink()
        else []
    )
    captured_at = _now()
    owner = (
        f"{outgoing.provider}:{outgoing.session_id}" if outgoing else "none bound"
    )
    text = "\n".join(
        [
            f"# Lane handoff: {label} after generation {lane['generation']}",
            "",
            f"- Captured: {captured_at}",
            f"- Lane: {label} ({lane['body_id']})",
            f"- Outgoing agent: {owner}",
            f"- Task: {task or 'none (the outgoing agent owned no task)'}",
            "",
            f"## Lane screen, last {len(screen)} lines",
            "",
            _fenced(screen),
            "",
            f"## .agent/chat_log.md, last {len(chat)} lines",
            "",
            _fenced(chat),
            "",
        ]
    )
    with sessions_root_lock(agent_dir, create=True) as sessions:
        handoffs = sessions / ".handoffs"
        if handoffs.is_symlink():
            raise SessionStateError("managed lane handoffs directory may not be a symlink")
        handoffs.mkdir(mode=0o700, exist_ok=True)
        stamp = captured_at.replace(":", "").replace("-", "").split(".")[0]
        path = handoffs / f"{lane['body_id']}-g{lane['generation']}-{stamp}.md"
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=handoffs
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
    return SwapHandoff(path=path, lane=label, outgoing=outgoing, task=task)


SWAP_PROMPT_PREFIX = "You are taking over lane "


def swap_handoff_prompt(handoff: SwapHandoff) -> str:
    """First message for the replacement agent.

    Only the replacement knows its own native key once it is running, so it
    claims the task itself with `--handoff-from`, the guarded exchange that
    succeeds only while the outgoing agent is still the recorded owner.
    """

    source = ""
    if handoff.outgoing is not None:
        source = f" from {handoff.outgoing.provider}:{handoff.outgoing.session_id}"
    read = (
        f"read `{handoff.path}` (this lane's last screen lines and the end of "
        "`.agent/chat_log.md`)"
    )
    opening = f"{SWAP_PROMPT_PREFIX}{handoff.lane}{source}. Run `pb-tasks bootstrap`"
    if handoff.task is None or handoff.outgoing is None:
        return f"{opening}, then {read}, then wait for the user."
    number = int(handoff.task)
    owner = f"{handoff.outgoing.provider}:{handoff.outgoing.session_id}"
    return (
        f"{opening}, then `pb-tasks work {number} --handoff-from {owner}`; "
        "if that refuses, stop and tell the user. "
        f"Then {read}, and continue task {number} from its current gate."
    )


def _running_elsewhere(keys: list[SessionKey]) -> set[SessionKey]:
    from tasks.resume import running_session_keys

    return running_session_keys(keys)


def swap_lane(
    agent_dir: Path,
    lane_address: str,
    *,
    provider: str,
    model: str | None = None,
    sandbox: bool | None = None,
    running: Callable[[list[SessionKey]], set[SessionKey]] = _running_elsewhere,
) -> tuple[ManagedLane, SwapHandoff]:
    """Hand a lane from its current agent to a new one, keeping the shell.

    Order matters: refuse while nothing has changed, then capture the screen
    while the outgoing agent still shows it, then stop that agent, then start
    the replacement. A failed start leaves the handoff file in place.
    """

    TmuxClient().require("pb-session")
    lane = settle_lane(agent_dir, resolve_managed_lane(agent_dir, lane_address))
    label = _lane_label(lane.record)
    state = lane.record["state"]
    if state == "destroyed":
        raise SessionStateError(
            f"lane {label} is gone (its tmux body ended); "
            f"use `pb-session resume {label}` to rebuild it"
        )
    if state not in {"shell_idle", "provider_exited", "provider_running"}:
        raise SessionStateError(f"lane {label} is {state}; wait for it or stop it first")
    try:
        selected = identity_spec(provider).provider
    except (TypeError, ValueError) as exc:
        raise SessionStateError(f"unknown interactive provider: {provider}") from exc
    unsupported = provider_support_error(selected, Path(lane.record["project"]))
    if unsupported:
        raise SessionStateError(f"managed {selected} session is unavailable: {unsupported}")
    outgoing = _lane_key(lane.record)
    if outgoing is not None and state != "provider_running" and outgoing in running([outgoing]):
        raise SessionStateError(
            f"{outgoing.provider}:{outgoing.session_id} still looks open in another "
            "terminal; close it before handing its lane over"
        )
    handoff = capture_swap_handoff(agent_dir, str(lane.record["body_id"]))
    if state == "provider_running":
        lane = stop_lane_provider(agent_dir, lane)
    if sandbox is None:
        sandbox = False
        if outgoing is not None:
            try:
                _path, record = resolve_session_record(
                    agent_dir, f"{outgoing.provider}:{outgoing.session_id}"
                )
                sandbox = bool(record.get("sandbox", False))
            except SessionStateError:
                pass
    lane, _reservation = dispatch_provider_generation(
        agent_dir=agent_dir,
        lane_address=str(lane.record["body_id"]),
        provider=selected,
        model=model,
        sandbox=sandbox,
        prompt=swap_handoff_prompt(handoff),
    )
    return lane, handoff


def _lane_or_none(
    agent_dir: Path, address: str, *, include_destroyed: bool = False
) -> ManagedLane | None:
    """The lane a human name or body ID names, if any; native keys name none."""

    if ":" in address:
        return None
    try:
        return resolve_managed_lane(
            agent_dir, address, include_destroyed=include_destroyed
        )
    except SessionStateError as exc:
        if str(exc).startswith("lane not found:"):
            return None
        raise


def _lane_of_record(agent_dir: Path, record: Mapping[str, object]) -> ManagedLane | None:
    """The lane a conversation last ran in (live or destroyed), if it had one."""

    body_id = record.get("body_id")
    if not isinstance(body_id, str):
        return None
    return _lane_or_none(agent_dir, body_id, include_destroyed=True)


def _settle_record(
    agent_dir: Path, record: dict, lane: ManagedLane | None
) -> dict:
    """Mark a `running` record stopped when nothing runs it any more."""

    if record.get("state") != "running":
        return record
    key = SessionKey.from_values(record["provider"], record["session_id"])
    if lane is not None:
        if lane.record["state"] == "provider_running" and _lane_key(lane.record) == key:
            return record
    elif not _terminal_body_state(record):
        return record
    body_id = record.get("body_id")
    _path, stopped = ensure_session_record(
        agent_dir,
        key.provider,
        key.session_id,
        enrich={"state": "stopped", "stopped_at": _now()},
        expected={"state": "running", "body_id": body_id},
        environment={},
    )
    _append_session_chronology(
        agent_dir,
        stopped,
        "stop",
        f"naturally terminated managed session body {body_id}",
        body_id,
        "natural",
        timestamp_field="stopped_at",
    )
    return stopped


def stop_session(agent_dir: Path, address: str) -> dict | ManagedLane:
    """Stop an agent. In a lane only the agent stops; the shell stays open."""

    lane = _lane_or_none(agent_dir, address)
    if lane is None:
        path, record = resolve_session_record(agent_dir, address)
        owner = _lane_of_record(agent_dir, record)
        if owner is not None:
            owner = settle_lane(agent_dir, owner)
            key = SessionKey.from_values(record["provider"], record["session_id"])
            if owner.record["state"] == "provider_running" and _lane_key(owner.record) == key:
                lane = owner
            else:
                # The conversation no longer runs in its lane.
                return resolve_session_record(agent_dir, address)[1]
    if lane is not None:
        lane = settle_lane(agent_dir, lane)
        if lane.record["state"] == "provider_running":
            return stop_lane_provider(agent_dir, lane)
        if lane.record["state"] in {"shell_idle", "provider_exited"}:
            return lane
        raise SessionStateError(
            f"lane {_lane_label(lane.record)} is {lane.record['state']}; nothing to stop"
        )
    return _stop_pre_lane_body(agent_dir, address)


def _stop_pre_lane_body(agent_dir: Path, address: str) -> dict:
    """Stop a body launched before lanes, where the agent was the tmux body."""

    path, record = resolve_session_record(agent_dir, address)
    body_id = record.get("body_id")
    if record.get("managed") is not True or not body_id:
        raise SessionStateError(f"session has no managed tmux body: {address}")
    if record.get("state") == "destroyed":
        raise SessionStateError("destroyed session cannot be stopped")
    if record.get("state") == "stopped":
        _append_session_chronology(
            agent_dir,
            record,
            "stop",
            f"explicitly stopped managed session body {body_id}",
            body_id,
            "explicit",
            timestamp_field="stopped_at",
        )
        return record
    if record.get("state") != "running":
        raise SessionStateError("managed stop requires a running session")
    terminal = stop_run(name=body_id, namespace=SESSION_TMUX_NAMESPACE)
    if terminal.get("state") not in TERMINAL_BODY_STATES:
        raise SessionStateError(
            f"tmux body did not reach a terminal state: {terminal.get('state')}"
        )
    current_path, current = resolve_session_record(
        agent_dir, f"{record['provider']}:{record['session_id']}"
    )
    if current_path != path or current.get("body_id") != body_id:
        raise SessionStateError("session body changed while stop was in progress")
    if current.get("state") != "running":
        raise SessionStateError("session lifecycle changed while stop was in progress")
    _path, stopped = ensure_session_record(
        agent_dir,
        current["provider"],
        current["session_id"],
        enrich={"state": "stopped", "stopped_at": _now()},
        expected={"state": "running", "body_id": body_id},
        environment={},
    )
    _append_session_chronology(
        agent_dir,
        stopped,
        "stop",
        f"explicitly stopped managed session body {body_id}",
        body_id,
        "explicit",
        timestamp_field="stopped_at",
    )
    return stopped


def _terminal_body_state(record: Mapping[str, object]) -> bool:
    body_id = record.get("body_id")
    if not isinstance(body_id, str):
        return False
    observed = status_run(name=body_id, namespace=SESSION_TMUX_NAMESPACE)
    return observed.get("state") in TERMINAL_BODY_STATES


def _attach_lane(lane: ManagedLane) -> None:
    result = attach_run(name=str(lane.record["body_id"]), namespace=SESSION_TMUX_NAMESPACE)
    if result != 0:
        raise SessionStateError(f"tmux attach exited with status {result}")


def _free_lane_name(agent_dir: Path, *names: object) -> str | None:
    for name in names:
        if isinstance(name, str) and _lane_or_none(agent_dir, name) is None:
            return name
    return None


def resume_session(
    agent_dir: Path,
    address: str,
    *,
    model: str | None = None,
    attach: bool = True,
    prompt: str | None = None,
) -> ManagedLane:
    """Resume one exact conversation in its lane, or in a new lane.

    The conversation's own lane is reused when its shell is idle. When that
    lane is gone (its tmux body ended, e.g. in a reboot) a new lane is built
    under the same name; when it is busy with another agent, under no name.
    Returns as soon as the resume command is typed into the shell.
    """

    # Preflight the optional machine dependency before reconciling or reserving
    # any durable session state. The agent can install it with user approval and
    # retry this exact command.
    TmuxClient().require("pb-session")
    lane = _lane_or_none(agent_dir, address, include_destroyed=True)
    key = None
    if lane is not None:
        lane = settle_lane(agent_dir, lane)
        key = _lane_key(lane.record)
    if key is None:
        # A lane that never ran a conversation (e.g. its shell failed to
        # start) does not hide a stopped conversation of the same name.
        try:
            _path, found = resolve_session_record(agent_dir, address)
        except SessionStateError:
            if lane is None:
                raise
            label = _lane_label(lane.record)
            raise SessionStateError(
                f"lane {label} has not run a conversation yet; "
                f"start one with `pb-session --name {label}`"
            ) from None
        key = SessionKey.from_values(found["provider"], found["session_id"])
        lane = _lane_of_record(agent_dir, found)
        if lane is not None:
            lane = settle_lane(agent_dir, lane)
    _path, record = resolve_session_record(agent_dir, f"{key.provider}:{key.session_id}")
    if record.get("managed") is not True:
        raise SessionStateError("only managed sessions can be resumed")
    record = _settle_record(agent_dir, record, lane)
    if record.get("state") != "stopped":
        where = f" in lane {_lane_label(lane.record)}" if lane is not None else ""
        raise SessionStateError(
            f"{key.provider}:{key.session_id} is already running{where}"
        )
    project_value = record.get("project")
    cwd_value = record.get("resume_cwd")
    if not isinstance(project_value, str) or not isinstance(cwd_value, str):
        raise SessionStateError("managed session lacks recorded project/resume cwd")
    project = Path(project_value).resolve()
    cwd = Path(cwd_value).resolve()
    if resolve_agent_dir(project).resolve() != agent_dir.resolve():
        raise SessionStateError("managed session project resolves to a different agent store")
    unsupported = provider_support_error(key.provider, project)
    if unsupported:
        raise SessionStateError(f"managed {key.provider} session is unavailable: {unsupported}")
    if lane is None or lane.record["state"] not in {"shell_idle", "provider_exited"}:
        name = _free_lane_name(
            agent_dir,
            lane.record["name"] if lane is not None else None,
            record.get("name"),
        )
        lane = create_managed_lane_body(
            agent_dir=agent_dir, project=project, cwd=cwd, name=name
        )
    lane, _reservation = dispatch_provider_generation(
        agent_dir=agent_dir,
        lane_address=str(lane.record["body_id"]),
        provider=key.provider,
        model=model,
        sandbox=bool(record.get("sandbox", False)),
        resume_session_id=key.session_id,
        prompt=prompt,
    )
    if attach:
        _attach_lane(lane)
    return lane


def adopt_session(
    agent_dir: Path,
    address: str,
    *,
    project_root: Path,
    cwd: Path | None = None,
    name: str | None = None,
    model: str | None = None,
    sandbox: bool = False,
    attach: bool = True,
) -> ManagedLane:
    """Bring one ad_hoc session under managed control and resume it in a lane.

    A session started outside ``pb-session`` leaves only an identity crumb:
    schema, provider, session_id and created_at. It records no project, no
    resume cwd and no managed state, so ``resume_session`` refuses it. Adoption
    supplies exactly those missing fields and then delegates to the ordinary
    resume path, which needs no changes to accept the completed record.

    Adoption is same-project by construction: the native conversation is
    resolved inside the caller's own agent store, never relocated between
    projects.

    The record transition is durable rather than atomic. Non-mutating checks all
    run first, so a refused adoption leaves the record untouched. Once the
    record is enriched, however, a later launch failure (tmux, provider
    handshake) leaves a normal *stopped managed* session — a valid state, not a
    corrupt one, which a plain ``pb-session resume`` retries.
    """

    # Preflight every non-mutating requirement before touching the record, so a
    # refusal cannot leave a half-adopted session behind.
    TmuxClient().require("pb-session")
    project = project_root.resolve()
    if resolve_agent_dir(project).resolve() != agent_dir.resolve():
        raise SessionStateError(
            "adoption is same-project: this session record belongs to a different agent store"
        )
    working = (cwd or project).resolve()
    if not working.is_dir():
        raise SessionStateError(f"adopt cwd is not a directory: {working}")
    # The lane cwd decides which project the provider's own hooks resolve, so a
    # foreign cwd would escape same-project adoption even with a valid record.
    if working != project and project not in working.parents:
        raise SessionStateError(
            f"adopt cwd must be the project root or below it: {working}"
        )
    safe_name = _validate_managed_name(name)
    # Checked before the sessions lock, which lane lookup takes itself.
    if safe_name is not None and _lane_or_none(agent_dir, safe_name) is not None:
        raise SessionStateError(f"session name is already in use: {safe_name}")

    with sessions_root_lock(agent_dir, create=True) as sessions:
        path, record = resolve_session_record(agent_dir, address, include_destroyed=True)
        if record.get("state") == "destroyed":
            raise SessionStateError("destroyed session cannot be adopted")
        if record.get("managed") is True:
            state = record.get("state") or "unknown"
            raise SessionStateError(
                f"session is already managed (state={state}); use resume, not adopt"
            )
        provider = record["provider"]
        unsupported = provider_support_error(provider, project)
        if unsupported:
            raise SessionStateError(f"managed {provider} session is unavailable: {unsupported}")
        key = SessionKey.from_values(provider, record["session_id"])
        assert_no_active_managed_resume(sessions, key)
        if safe_name is not None:
            for other_path, other in list_session_records(agent_dir):
                if other_path != path and other.get("name") == safe_name:
                    raise SessionStateError(f"session name is already in use: {safe_name}")
            launches = _launches_directory(sessions)
            for launch_path in launches.glob("*.json"):
                launch = _validate_launch(
                    json.loads(launch_path.read_text(encoding="utf-8")), launch_path
                )
                if launch.get("name") == safe_name:
                    raise SessionStateError(f"session name is already reserved: {safe_name}")

        enrich: dict[str, object] = {
            "managed": True,
            "state": "stopped",
            "project": str(project),
            "resume_cwd": str(working),
            "sandbox": bool(sandbox),
        }
        if safe_name is not None:
            enrich["name"] = safe_name
        _adopted_path, adopted = ensure_session_record(
            agent_dir, provider, record["session_id"], enrich=enrich, environment={}
        )

    _append_session_chronology(
        agent_dir,
        adopted,
        "adopt",
        f"adopted ad_hoc session into managed control (cwd {working})",
        "ad_hoc",
        "stopped",
    )
    return resume_session(
        agent_dir,
        f"{provider}:{record['session_id']}",
        model=model,
        attach=attach,
    )


def rename_session(agent_dir: Path, address: str, new_name: str) -> dict:
    safe_name = _validate_managed_name(new_name)
    assert safe_name is not None
    with sessions_root_lock(agent_dir, create=True) as sessions:
        path, record = resolve_session_record(agent_dir, address)
        if record.get("state") == "destroyed":
            raise SessionStateError("destroyed session cannot be renamed")
        key = SessionKey.from_values(record["provider"], record["session_id"])
        assert_no_active_managed_resume(sessions, key)
        for other_path, other in list_session_records(agent_dir):
            if other_path != path and other.get("name") == safe_name:
                raise SessionStateError(f"session name is already in use: {safe_name}")
        launches = _launches_directory(sessions)
        for launch_path in launches.glob("*.json"):
            launch = _validate_launch(
                json.loads(launch_path.read_text(encoding="utf-8")), launch_path
            )
            if launch.get("name") == safe_name:
                raise SessionStateError(f"session name is already reserved: {safe_name}")
        renamed_path, renamed = ensure_session_record(
            agent_dir,
            record["provider"],
            record["session_id"],
            enrich={"name": safe_name},
            environment={},
        )
        old_name = record.get("name")
    if old_name != safe_name:
        _append_session_chronology(
            agent_dir,
            renamed,
            "rename",
            f"renamed managed session from {old_name or '-'} to {safe_name}",
            old_name or "-",
            safe_name,
            renamed_path.stat().st_mtime_ns,
        )
    return renamed


def destroy_session(agent_dir: Path, address: str) -> dict | ManagedLane:
    """Close a lane's shell, or tombstone one stopped conversation record.

    A lane name closes that lane; its conversations stay resumable. A native
    key tombstones that conversation, as before lanes existed.
    """

    lane = _lane_or_none(agent_dir, address)
    if lane is not None:
        lane = settle_lane(agent_dir, lane)
        label = _lane_label(lane.record)
        if lane.record["state"] == "destroyed":
            return lane
        if lane.record["state"] not in {"shell_idle", "provider_exited"}:
            raise SessionStateError(
                f"lane {label} still runs an agent; `pb-session stop {label}` first"
            )
        stop_run(name=str(lane.record["body_id"]), namespace=SESSION_TMUX_NAMESPACE)
        return _lane_exited(agent_dir, lane, state="destroyed")
    with sessions_root_lock(agent_dir, create=True) as sessions:
        _path, record = resolve_session_record(agent_dir, address)
        if record.get("state") != "stopped":
            raise SessionStateError("session must be stopped before destroy")
        key = SessionKey.from_values(record["provider"], record["session_id"])
        assert_no_active_managed_resume(sessions, key)
        _path, destroyed = ensure_session_record(
            agent_dir,
            record["provider"],
            record["session_id"],
            enrich={"state": "destroyed", "destroyed_at": _now()},
            environment={},
        )
    _append_session_chronology(
        agent_dir,
        destroyed,
        "destroy",
        "destroyed managed session record",
        record.get("body_id"),
        timestamp_field="destroyed_at",
    )
    return destroyed


def start_managed_session(
    *,
    project_root: Path | None = None,
    name: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    sandbox: bool = False,
    attach: bool = True,
    prompt: str | None = None,
) -> ManagedLane:
    """Open a shell lane and type the provider's start command into it.

    Returns once the command is typed; the provider binds its native identity
    on its own. An idle lane with this name is reused; a busy one is refused.
    """

    # Keep project initialization machine-neutral; diagnose tmux only when the
    # user first asks for a managed body, before a launch reservation exists.
    TmuxClient().require("pb-session")

    if project_root is None:
        from tasks.cli import find_project_root
        project = find_project_root().resolve()
    else:
        project = project_root.resolve()
    agent_dir = resolve_agent_dir(project)
    launch_cwd = Path.cwd().resolve() if project_root is None else project
    selected = resolve_start_provider(agent_dir, project, provider)
    lane = _lane_or_none(agent_dir, name) if name is not None else None
    if lane is not None:
        lane = settle_lane(agent_dir, lane)
        state = lane.record["state"]
        if state == "destroyed":
            lane = None
        elif state not in {"shell_idle", "provider_exited"}:
            raise SessionStateError(
                f"lane {name} is busy ({state}); `pb-session swap {name} <provider>` "
                f"replaces its agent and `pb-session stop {name}` stops it"
            )
    if lane is None:
        try:
            lane = create_managed_lane_body(
                agent_dir=agent_dir, project=project, cwd=launch_cwd, name=name
            )
        except KeyboardInterrupt:
            raise SessionStateError("managed session start interrupted; lane closed") from None
    label = _lane_label(lane.record)
    try:
        lane, _reservation = dispatch_provider_generation(
            agent_dir=agent_dir,
            lane_address=str(lane.record["body_id"]),
            provider=selected,
            model=model,
            sandbox=sandbox,
            prompt=prompt,
        )
    except (SessionStateError, TmuxAgentError, KeyboardInterrupt) as exc:
        reason = "start interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)
        raise SessionStateError(
            f"{reason}; lane {label} stays open as a shell (`pb-session attach {label}`)"
        ) from None
    if attach:
        _attach_lane(lane)
    return lane


def _count(minimum: int):
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected a whole number, not {text!r}")
        if value < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum}")
        return value

    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pb-session",
        description="Start and manage provider-native Playbook sessions.",
    )
    parser.add_argument("--name")
    parser.add_argument("--provider", choices=("claude", "codex", "agy", "pi", "omp"))
    parser.add_argument("--model")
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="run the provider body under Playbook write containment",
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help=(
            "start without attaching; first-run trust/hook-review prompts fail "
            "fast with an interactive rerun command"
        ),
    )
    commands = parser.add_subparsers(dest="command_name")
    listing = commands.add_parser(
        "list", help="list sessions with recorded lifecycle and observed body state"
    )
    listing.add_argument("--all", action="store_true", help="include destroyed history")
    listing.add_argument("--verbose", action="store_true", help="show orientation details")
    listing.add_argument("--json", action="store_true")
    status = commands.add_parser(
        "status", help="show reboot orientation and the exact manual resume route"
    )
    status.add_argument("address")
    status.add_argument("--history", action="store_true", help="allow a destroyed native key")
    status.add_argument("--json", action="store_true")
    attach = commands.add_parser("attach", help="attach to one managed body")
    attach.add_argument("address")
    peek = commands.add_parser("peek", help="capture recent pane text")
    peek.add_argument("address")
    peek.add_argument("lines", nargs="?", type=int, default=50)
    send = commands.add_parser("send", help="send literal text and a separate Enter")
    send.add_argument("address")
    send.add_argument("message")
    send.add_argument("--no-enter", action="store_true")
    resume = commands.add_parser("resume", help="resume the exact native conversation")
    resume.add_argument("address")
    resume.add_argument("--attach", action="store_true")
    resume.add_argument("--model")
    resume.add_argument(
        "--prompt",
        help="first message for the resumed conversation (default: bootstrap, then wait)",
    )
    adopt = commands.add_parser(
        "adopt",
        help="bring an ad_hoc session under managed control and resume it in a lane",
        description=(
            "Adopt one session that was started outside pb-session (recorded as "
            "ad_hoc) and resume that exact conversation in a managed tmux lane. "
            "Same-project only: the session is resolved in this project's own "
            "agent store. Exit the session in its original terminal first. "
            "The transition is durable rather than atomic: refusals leave the "
            "record untouched, but once adopted, a failed launch leaves a normal "
            "stopped managed session that plain `pb-session resume` retries."
        ),
    )
    adopt.add_argument("address")
    adopt.add_argument("--cwd", help="working directory for the lane (default: project root)")
    adopt.add_argument("--name", help="human lane name, e.g. lane-x")
    adopt.add_argument("--attach", action="store_true")
    adopt.add_argument("--model")
    stop = commands.add_parser(
        "stop", help="stop a lane's agent; the lane's shell stays open"
    )
    stop.add_argument("address")
    swap = commands.add_parser(
        "swap",
        help="hand a lane to a new agent, keeping its shell",
        description=(
            "Save the lane's last 500 screen lines and the last 200 lines of "
            ".agent/chat_log.md to a handoff file, stop the lane's agent, and "
            "start PROVIDER in the same shell. Its first message tells it to "
            "claim the outgoing agent's task with `pb-tasks work N "
            "--handoff-from <key>` and read the handoff file."
        ),
    )
    swap.add_argument("address", help="lane name or body ID")
    swap.add_argument(
        "swap_provider",
        metavar="provider",
        nargs="?",
        choices=("claude", "codex", "agy", "pi", "omp"),
    )
    swap.add_argument(
        "--provider",
        dest="swap_provider_flag",
        choices=("claude", "codex", "agy", "pi", "omp"),
        help="same as the positional provider",
    )
    swap.add_argument("--model")
    swap.add_argument("--attach", action="store_true")
    rename = commands.add_parser("rename", help="change only the human session name")
    rename.add_argument("address")
    rename.add_argument("new_name")
    destroy = commands.add_parser(
        "destroy",
        help="close an idle lane's shell, or tombstone one stopped conversation",
    )
    destroy.add_argument("address")
    restore = commands.add_parser(
        "restore",
        help="pick one of this project's recent conversations and resume it here",
        description=(
            "Without arguments, list the conversations this project's chat log saw "
            "recently, newest first. With a task number (or the start of a session "
            "ID), resume that conversation in this terminal with the provider's own "
            "resume command."
        ),
    )
    restore.add_argument(
        "selector", nargs="?", help="task number, lane name, or the start of a session ID"
    )
    restore.add_argument(
        "--days", type=_count(1), default=10, help="how far back to look (default: 10)"
    )
    restore.add_argument(
        "--gates", type=_count(0), default=5,
        help="last checked gates shown per session (default: 5; 0 hides them)",
    )
    restore.add_argument(
        "--messages", type=_count(0), default=5,
        help="your last messages shown per session (default: 5; 0 hides them)",
    )
    restore.add_argument(
        "--width", type=_count(40),
        help="clip report lines to this many columns (default: terminal width)",
    )
    restore.add_argument(
        "--force", action="store_true", help="resume even if it looks open in another terminal"
    )
    return parser


def _restore(options: argparse.Namespace) -> int:
    from datetime import datetime, timezone
    import shutil

    from tasks.cli import find_project_root
    from tasks.resume import ResumeError, exec_plan, run_restore

    project_root = find_project_root().resolve()
    try:
        plan = run_restore(
            resolve_agent_dir(project_root),
            project_root,
            options.selector,
            now=datetime.now(timezone.utc),
            write=print,
            days=options.days,
            force=options.force,
            width=options.width or shutil.get_terminal_size((100, 24)).columns,
            messages=options.messages,
            gates=options.gates,
        )
    except ResumeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if plan is None:
        return 0
    print(f"Resuming: {plan.shell_line()}", flush=True)
    exec_plan(plan, project_root)


def _current_agent_dir() -> Path:
    from tasks.cli import find_project_root
    return resolve_agent_dir(find_project_root().resolve())


def _print_status(value: Mapping[str, object]) -> None:
    lifecycle = value["recorded_lifecycle"]
    body = value["observed_body"]
    current = value.get("current_task") or "unclaimed"
    gate = value.get("current_gate")
    latest = value.get("latest_recorded_task") or "none"
    detail = (
        f" exit={body['exit_status']}" if body.get("exit_status") is not None else ""
    )
    print(
        f"{value['address']} | {value['provider']}:{value['session_id']} | "
        f"recorded={lifecycle['state']} | observed-body={body['state']}{detail} | "
        f"current-task={current} | latest-recorded-task={latest}"
    )
    if isinstance(gate, Mapping):
        print(
            f"  current-gate: {gate['completed']}/{gate['total']} "
            f"line={gate['line']} {gate['text']}"
        )
    activity = value.get("latest_recorded_activity")
    if isinstance(activity, Mapping):
        print(
            f"  latest-recorded-activity: {activity['marker']} "
            f"[{activity['timestamp']}] {activity['body']}"
        )
    lane = value.get("lane")
    if isinstance(lane, Mapping):
        whose = "runs or last ran" if lane["current"] else "has since moved on from"
        print(
            f"  lane: {lane['name'] or lane['body_id']} ({lane['body_id']}) "
            f"state={lane['state']} generation={lane['generation']} "
            f"agent={lane['agent'] or '-'}; the lane {whose} this conversation"
        )
        if lane.get("attention"):
            print(f"  attention: {lane['attention']}")
    if value.get("resume_command"):
        print(f"  resume: {value['resume_command']}")
    print(f"  chronology: {value['chronology_note']}")
    for diagnostic in value.get("diagnostics", []):
        print(f"  diagnostic: {diagnostic}")


def _print_summary(value: Mapping[str, object]) -> None:
    lane = value.get("lane")
    # Before lanes the body was the agent; in a lane the shell outlives it,
    # so the conversation's own lifecycle is the honest column.
    state = value["lifecycle_state"] if isinstance(lane, Mapping) else value["observed_body"]["state"]
    task = value.get("current_task") or "-"
    line = f"{value['address']}\t{value['provider']}\t{state}\ttask={task}"
    if isinstance(lane, Mapping) and lane["current"]:
        line += f"\tlane={lane['state']}"
    print(line)


def main(argv: list[str] | None = None) -> int:
    options = _parser().parse_args(argv)
    try:
        if options.command_name == "list":
            values = list_session_status(
                _current_agent_dir(), include_destroyed=options.all
            )
            if options.json:
                print(json.dumps(values, indent=2, sort_keys=True))
            else:
                for value in values:
                    if options.verbose:
                        _print_status(value)
                    else:
                        _print_summary(value)
            return 0
        if options.command_name == "status":
            value = session_status(
                _current_agent_dir(),
                options.address,
                include_destroyed=options.history,
            )
            if options.json:
                print(json.dumps(value, indent=2, sort_keys=True))
            else:
                _print_status(value)
            return 0
        if options.command_name == "attach":
            return attach_session(_current_agent_dir(), options.address)
        if options.command_name == "restore":
            return _restore(options)
        if options.command_name == "peek":
            sys.stdout.write(
                peek_session(_current_agent_dir(), options.address, lines=options.lines)
            )
            return 0
        if options.command_name == "send":
            send_session(
                _current_agent_dir(),
                options.address,
                options.message,
                enter=not options.no_enter,
            )
            return 0
        if options.command_name == "resume":
            lane = resume_session(
                _current_agent_dir(),
                options.address,
                model=options.model,
                attach=options.attach,
                prompt=options.prompt,
            )
            if not options.attach:
                print(_lane_line(lane, "resuming"))
            return 0
        if options.command_name == "swap":
            chosen = {options.swap_provider, options.swap_provider_flag} - {None}
            if len(chosen) != 1:
                raise SessionStateError(
                    f"name one provider: `pb-session swap {options.address} <provider>`"
                )
            lane, handoff = swap_lane(
                _current_agent_dir(),
                options.address,
                provider=chosen.pop(),
                model=options.model,
            )
            outgoing = (
                f"{handoff.outgoing.provider}:{handoff.outgoing.session_id}"
                if handoff.outgoing
                else "an unbound agent"
            )
            claim = f"; it will claim task {int(handoff.task)}" if handoff.task else ""
            print(f"{_lane_label(lane.record)}: handed over from {outgoing}{claim}")
            print(f"  handoff: {handoff.path}")
            print(_lane_line(lane, "starting"))
            if options.attach:
                _attach_lane(lane)
            return 0
        if options.command_name == "adopt":
            from tasks.cli import find_project_root
            lane = adopt_session(
                _current_agent_dir(),
                options.address,
                project_root=find_project_root().resolve(),
                cwd=Path(options.cwd) if options.cwd else None,
                name=options.name,
                model=options.model,
                sandbox=options.sandbox,
                attach=options.attach,
            )
            print(f"adopted into {_lane_line(lane, 'resuming')}")
            return 0
        if options.command_name == "stop":
            stopped = stop_session(_current_agent_dir(), options.address)
            if isinstance(stopped, ManagedLane):
                print(
                    f"{_lane_label(stopped.record)}: agent stopped; the shell stays open "
                    f"(`pb-session destroy {_lane_label(stopped.record)}` closes it)"
                )
            else:
                address = stopped.get("name") or f"{stopped['provider']}:{stopped['session_id']}"
                print(f"{address} stopped")
            return 0
        if options.command_name == "rename":
            record = rename_session(
                _current_agent_dir(), options.address, options.new_name
            )
            print(f"{record['provider']}:{record['session_id']} named {record['name']}")
            return 0
        if options.command_name == "destroy":
            destroyed = destroy_session(_current_agent_dir(), options.address)
            if isinstance(destroyed, ManagedLane):
                print(f"{_lane_label(destroyed.record)}: lane closed")
            else:
                print(f"{destroyed['provider']}:{destroyed['session_id']} destroyed")
            return 0
        lane = start_managed_session(
            name=options.name,
            provider=options.provider,
            model=options.model,
            sandbox=options.sandbox,
            attach=not options.detach,
        )
    except (SessionStateError, TmuxAgentError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if options.detach:
        print(_lane_line(lane, "starting"))
    return 0


def _lane_line(lane: ManagedLane, verb: str) -> str:
    record = lane.record
    label = _lane_label(record)
    return (
        f"{label}: shell ready, {record['launch_provider']} {verb} "
        f"(generation {record['generation']}, {record['body_id']}); "
        f"`pb-session attach {label}` to watch"
    )


if __name__ == "__main__":
    raise SystemExit(main())
