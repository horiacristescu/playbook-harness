"""Managed Playbook session lifecycle composition.

The public CLI grows here in vertical slices. Provider-native identity remains
owned by provider hooks; this module only carries a short-lived launch
capability into a Playbook-owned tmux body.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Mapping

from provider.session_identity import identity_spec, scrub_inherited_session_identity
from provider.sandbox import bypass_args, wrap_command_argv
from provider.session_state import (
    ManagedLaunchReservation,
    SessionKey,
    SessionStateError,
    _atomic_json,
    _launches_directory,
    _validate_launch,
    _validate_managed_name,
    _validate_record,
    assert_no_active_managed_resume,
    cancel_managed_launch,
    ensure_session_record,
    iter_session_directories,
    reserve_managed_launch,
    session_directory,
    sessions_root_lock,
)
from tasks.core import resolve_agent_dir
from tmux_agent import (
    TmuxClient,
    TmuxAgentError,
    attach_run,
    label_run,
    peek_run,
    resolve_agent_command,
    send_message,
    start_run,
    status_run,
    stop_run,
)


BOOTSTRAP_PROMPT = "Run `pb-tasks bootstrap`, then wait."
SESSION_TMUX_NAMESPACE = "sessions"
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


def _detached_attention_error(
    reservation: ManagedLaunchReservation,
    provider: str,
    model: str | None,
    attention: str,
) -> SessionStateError:
    import shlex

    command = ["pb-session"]
    if reservation.name is not None:
        command.extend(["--name", reservation.name])
    command.extend(["--provider", provider])
    if model is not None:
        command.extend(["--model", model])
    return SessionStateError(
        f"provider is waiting for {attention}; detached start cannot make this "
        "security decision. The provisional body was stopped. Review it "
        f"interactively by rerunning without --detach: {shlex.join(command)}"
    )


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
        }
    )
    return result


def _find_body_record(agent_dir: Path, body_id: str) -> tuple[Path, dict] | None:
    matches: list[tuple[Path, dict]] = []
    for key, directory in iter_session_directories(agent_dir):
        path = directory / "session.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("managed") is True and record.get("body_id") == body_id:
            matches.append((path, record))
    if len(matches) > 1:
        raise SessionStateError(f"managed body ID is bound to multiple sessions: {body_id}")
    return matches[0] if matches else None


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


def session_status(
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

    resume_cwd = record.get("resume_cwd")
    resume_command = None
    if lifecycle != "destroyed":
        if not isinstance(resume_cwd, str) or not resume_cwd:
            diagnostics.append(
                "manual resume route is incomplete: session has no recorded resume_cwd"
            )
        else:
            import shlex

            qualified = f"{record['provider']}:{record['session_id']}"
            if record.get("managed") is True:
                resume_argv = ["pb-session", "resume", qualified]
            else:
                adapter = _adapter(record["provider"], Path(record.get("project") or resume_cwd))
                resume_argv = resolve_agent_command(
                    _PROVIDER_AGENTS[record["provider"]],
                    adapter.interactive_argv(
                        prompt=BOOTSTRAP_PROMPT,
                        resume_session_id=record["session_id"],
                    ),
                )
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


def _managed_body(agent_dir: Path, address: str) -> tuple[Path, dict, str]:
    path, record = resolve_session_record(agent_dir, address)
    body_id = record.get("body_id")
    if record.get("managed") is not True or not body_id:
        raise SessionStateError(f"session has no managed tmux body: {address}")
    return path, record, body_id


def attach_session(agent_dir: Path, address: str) -> int:
    _path, _record, body_id = _managed_body(agent_dir, address)
    return attach_run(name=body_id, namespace=SESSION_TMUX_NAMESPACE)


def peek_session(agent_dir: Path, address: str, *, lines: int = 50) -> str:
    _path, _record, body_id = _managed_body(agent_dir, address)
    return peek_run(name=body_id, namespace=SESSION_TMUX_NAMESPACE, lines=lines)


def send_session(agent_dir: Path, address: str, message: str, *, enter: bool = True) -> None:
    _path, _record, body_id = _managed_body(agent_dir, address)
    send_message(
        name=body_id,
        namespace=SESSION_TMUX_NAMESPACE,
        message=message,
        enter=enter,
    )


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


def _launch_reserved_session(
    *,
    agent_dir: Path,
    project: Path,
    cwd: Path,
    provider: str,
    reservation: ManagedLaunchReservation,
    model: str | None,
    resume_session_id: str | None,
    sandbox: bool,
    attach: bool,
    handshake_timeout: float,
) -> dict:
    adapter = _adapter(provider, project)
    environment = managed_launch_environment(
        reservation, project, provider, os.environ
    )
    agent_name = _PROVIDER_AGENTS[provider]
    agent_args = adapter.interactive_argv(
        prompt=BOOTSTRAP_PROMPT,
        model=model,
        resume_session_id=resume_session_id,
    )
    if sandbox:
        exact_command = resolve_agent_command(agent_name, agent_args)
        permission_args = (
            bypass_args(agent_name)
            if agent_name in {"claude", "codex", "agy", "pi"}
            else []
        )
        exact_command = [exact_command[0], *permission_args, *exact_command[1:]]
        agent_name = "command"
        agent_args = wrap_command_argv(exact_command, project)
        environment["PLAYBOOK_SANDBOXED"] = "1"
    metadata: dict | None = None
    try:
        metadata = start_run(
            name=reservation.body_id,
            namespace=SESSION_TMUX_NAMESPACE,
            agent=agent_name,
            agent_args=agent_args,
            cwd=cwd,
            environment=environment,
            model=None,
        )
        if reservation.name is not None:
            label_run(
                name=reservation.body_id,
                namespace=SESSION_TMUX_NAMESPACE,
                label=reservation.name,
            )
        deadline = time.monotonic() + handshake_timeout
        bound = _find_body_record(agent_dir, reservation.body_id)
        attached_attention: set[str] = set()
        did_attach = False
        while bound is None and time.monotonic() < deadline:
            body = status_run(
                name=reservation.body_id,
                namespace=SESSION_TMUX_NAMESPACE,
            )
            if body.get("state") in TERMINAL_BODY_STATES:
                raise SessionStateError(
                    "provider body terminated before native identity binding: "
                    f"body={reservation.body_id} state={body.get('state')} "
                    f"exit={body.get('exit_status')}"
                )
            try:
                pane_text = peek_run(
                    name=reservation.body_id,
                    namespace=SESSION_TMUX_NAMESPACE,
                    lines=80,
                )
            except (TmuxAgentError, OSError):
                pane_text = ""
            attention = _startup_attention(pane_text)
            if attention is not None and attention not in attached_attention:
                if not attach:
                    raise _detached_attention_error(
                        reservation, provider, model, attention
                    )
                attached_attention.add(attention)
                result = attach_run(
                    name=reservation.body_id,
                    namespace=SESSION_TMUX_NAMESPACE,
                )
                did_attach = True
                if result != 0:
                    raise SessionStateError(
                        f"tmux attach for {attention} exited with status {result}"
                    )
            time.sleep(0.05)
            bound = _find_body_record(agent_dir, reservation.body_id)
        if bound is None:
            raise SessionStateError(
                "provider started but did not bind its native session ID before the "
                f"{handshake_timeout:g}s handshake deadline; body={reservation.body_id}"
            )
    except BaseException as exc:
        cancel_managed_launch(agent_dir, reservation)
        if metadata is not None:
            try:
                stop_run(
                    name=reservation.body_id,
                    namespace=SESSION_TMUX_NAMESPACE,
                )
            except TmuxAgentError as cleanup_exc:
                raise SessionStateError(
                    "managed session launch failed and its body could not be stopped: "
                    f"body={reservation.body_id}; cleanup={cleanup_exc}"
                ) from exc
        if isinstance(exc, KeyboardInterrupt):
            raise SessionStateError(
                f"managed session start interrupted; body={reservation.body_id} stopped"
            ) from None
        raise
    path, record = bound
    record_path, record = ensure_session_record(
        agent_dir,
        record["provider"],
        record["session_id"],
        enrich={
            "tmux_session": metadata.get("tmux_session"),
            "tmux_pane": metadata.get("pane_id"),
        },
        environment={},
    )
    assert record_path == path
    _append_session_chronology(
        agent_dir,
        record,
        "bind",
        f"bound managed body {reservation.body_id} to native session",
        reservation.body_id,
        timestamp_field="last_started_at",
    )
    operation = "resume" if resume_session_id is not None else "start"
    _append_session_chronology(
        agent_dir,
        record,
        operation,
        f"started managed session body {reservation.body_id}"
        if operation == "start"
        else f"resumed managed session body {reservation.body_id}",
        reservation.body_id,
        timestamp_field="last_started_at",
    )
    if attach and not did_attach:
        result = attach_run(
            name=reservation.body_id,
            namespace=SESSION_TMUX_NAMESPACE,
        )
        if result != 0:
            raise SessionStateError(f"tmux attach exited with status {result}")
    return record


def stop_session(agent_dir: Path, address: str) -> dict:
    path, record, body_id = _managed_body(agent_dir, address)
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


def resume_session(
    agent_dir: Path,
    address: str,
    *,
    model: str | None = None,
    attach: bool = True,
    handshake_timeout: float = 120.0,
) -> dict:
    # Preflight the optional machine dependency before reconciling or reserving
    # any durable session state. The agent can install it with user approval and
    # retry this exact command.
    TmuxClient().require("pb-session")
    path, record = resolve_session_record(agent_dir, address)
    if record.get("managed") is not True:
        raise SessionStateError("only managed sessions can be resumed")
    if record.get("state") == "running" and _terminal_body_state(record):
        old_body_id = record.get("body_id")
        _path, record = ensure_session_record(
            agent_dir,
            record["provider"],
            record["session_id"],
            enrich={"state": "stopped", "stopped_at": _now()},
            expected={"state": "running", "body_id": record.get("body_id")},
            environment={},
        )
        _append_session_chronology(
            agent_dir,
            record,
            "stop",
            f"naturally terminated managed session body {old_body_id}",
            old_body_id,
            "natural",
            timestamp_field="stopped_at",
        )
    if record.get("state") != "stopped":
        raise SessionStateError("managed resume requires a stopped session")
    project_value = record.get("project")
    cwd_value = record.get("resume_cwd")
    if not isinstance(project_value, str) or not isinstance(cwd_value, str):
        raise SessionStateError("managed session lacks recorded project/resume cwd")
    project = Path(project_value).resolve()
    cwd = Path(cwd_value).resolve()
    if resolve_agent_dir(project).resolve() != agent_dir.resolve():
        raise SessionStateError("managed session project resolves to a different agent store")
    provider = record["provider"]
    unsupported = provider_support_error(provider, project)
    if unsupported:
        raise SessionStateError(f"managed {provider} session is unavailable: {unsupported}")
    key = SessionKey.from_values(provider, record["session_id"])
    reservation = reserve_managed_launch(
        agent_dir,
        project_root=project,
        provider=provider,
        name=record.get("name"),
        cwd=cwd,
        model=model,
        sandbox=bool(record.get("sandbox", False)),
        operation="resume",
        expected_key=key,
    )
    return _launch_reserved_session(
        agent_dir=agent_dir,
        project=project,
        cwd=cwd,
        provider=provider,
        reservation=reservation,
        model=model,
        resume_session_id=key.session_id,
        sandbox=bool(record.get("sandbox", False)),
        attach=attach,
        handshake_timeout=handshake_timeout,
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


def destroy_session(agent_dir: Path, address: str) -> dict:
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
    handshake_timeout: float = 120.0,
) -> dict:
    """Start one tmux body and wait for its provider-native identity binding."""

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
    reservation = reserve_managed_launch(
        agent_dir,
        project_root=project,
        provider=selected,
        name=name,
        cwd=launch_cwd,
        model=model,
        sandbox=sandbox,
    )
    return _launch_reserved_session(
        agent_dir=agent_dir,
        project=project,
        cwd=launch_cwd,
        provider=selected,
        reservation=reservation,
        model=model,
        resume_session_id=None,
        sandbox=sandbox,
        attach=attach,
        handshake_timeout=handshake_timeout,
    )


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
    stop = commands.add_parser("stop", help="stop one managed body, preserving session state")
    stop.add_argument("address")
    rename = commands.add_parser("rename", help="change only the human session name")
    rename.add_argument("address")
    rename.add_argument("new_name")
    destroy = commands.add_parser("destroy", help="tombstone one stopped session")
    destroy.add_argument("address")
    return parser


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
    if value.get("resume_command"):
        print(f"  resume: {value['resume_command']}")
    print(f"  chronology: {value['chronology_note']}")
    for diagnostic in value.get("diagnostics", []):
        print(f"  diagnostic: {diagnostic}")


def _print_summary(value: Mapping[str, object]) -> None:
    body = value["observed_body"]
    task = value.get("current_task") or "-"
    print(f"{value['address']}\t{value['provider']}\t{body['state']}\ttask={task}")


def main(argv: list[str] | None = None) -> int:
    options = _parser().parse_args(argv)
    try:
        if options.command_name == "list":
            agent_dir = _current_agent_dir()
            values = [
                session_status(
                    agent_dir,
                    f"{record['provider']}:{record['session_id']}",
                    include_destroyed=options.all,
                )
                for _path, record in list_session_records(
                    agent_dir, include_destroyed=options.all
                )
            ]
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
            record = resume_session(
                _current_agent_dir(),
                options.address,
                model=options.model,
                attach=options.attach,
            )
            address = record.get("name") or f"{record['provider']}:{record['session_id']}"
            print(f"{address} running ({record['body_id']})")
            return 0
        if options.command_name == "stop":
            record = stop_session(_current_agent_dir(), options.address)
            address = record.get("name") or f"{record['provider']}:{record['session_id']}"
            print(f"{address} stopped")
            return 0
        if options.command_name == "rename":
            record = rename_session(
                _current_agent_dir(), options.address, options.new_name
            )
            print(f"{record['provider']}:{record['session_id']} named {record['name']}")
            return 0
        if options.command_name == "destroy":
            record = destroy_session(_current_agent_dir(), options.address)
            print(f"{record['provider']}:{record['session_id']} destroyed")
            return 0
        record = start_managed_session(
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
        address = record.get("name") or f"{record['provider']}:{record['session_id']}"
        print(f"{address} running ({record['body_id']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
