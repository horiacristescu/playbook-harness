"""Resume a task's agent conversation by task number.

After a reboot the task number is the handle a person remembers, and task.md
already names the provider-native session that owns the work (`## Sessions`,
latest entry last). Everything here is derived from task.md, session records,
chat_log.md and the provider's own transcript store; nothing new is persisted.

Lanes are tmux boxes owned by a monitor, so `resume monitor` brings back only
the monitor. Its prompt tells it to recover its lanes from its own board.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, NoReturn

from provider.session_identity import scrub_inherited_session_identity
from provider.session_state import SessionKey, SessionStateError, session_directory
from tasks.task_document import (
    TaskAuthorityMismatch,
    TaskDocument,
    TaskDocumentError,
    resolve_task_document,
)

MONITOR_SELECTOR = "monitor"
RESTORE_DAYS = 10
RESTORE_MESSAGES = 5
RESTORE_GATES = 5
_MESSAGE_WIDTH = 96
RESTORE_PROMPT = (
    "Resumed with `pb-session restore` after a restart. "
    "Run `pb-tasks status`, then wait for the user."
)
# Rows captured as M entries that the person did not type: harness injections
# and the first prompts that resume/launch commands hand to the agent. Other
# bodies starting with "<" wrap real input (<pasted_content>, voice
# <realtime_delegation>, IDE context) and are kept, with the tags stripped.
_SYNTHETIC_MESSAGES = (
    "<task-notification",
    "<cross-session-message",
    "<agent-message",
    "<command-message",
    "[Request interrupted by user]",
    "Resumed with `pb-tasks resume",
    "Resumed with `pb-session restore",
    "Run `pb-tasks bootstrap`, then wait.",
    # session_cli.SWAP_PROMPT_PREFIX: the first message after `pb-session swap`.
    "You are taking over lane ",
    # Review-judge prompts: `=== <file> ===` context blocks, or the reviewer
    # persona from the review templates in tasks/template.py.
    "=== ",
    "You are a senior engineer reviewing a ",
)
_IDE_CONTEXT_RE = re.compile(r"<(ide_selection|ide_opened_file)\b[^>]*>.*?</\1>", re.DOTALL)
_TAG_RE = re.compile(r"</?[A-Za-z][\w-]*(?:\s[^<>]*)?>")
_TASK_MARKER_RE = re.compile(r"^[GT](\d+):")
_CHECKED_GATE_RE = re.compile(r"^- \[[xX]\]\s*")
# A plain resume must not look like a managed launch to the provider's hooks
# (`bind_managed_launch` keys on these), or it would demand a tmux handshake.
_MANAGED_LAUNCH_ENV = (
    "PLAYBOOK_MANAGED_LAUNCH_TOKEN",
    "PLAYBOOK_MANAGED_BODY_ID",
    "PLAYBOOK_MANAGED_PROJECT_ROOT",
)


class ResumeError(Exception):
    """A resume request that cannot be honored as asked."""


@dataclass(frozen=True)
class ResumeTarget:
    task_number: str
    task_name: str
    key: SessionKey
    monitor: bool
    via: str  # what the person typed after `pb-tasks resume`, normalized


@dataclass(frozen=True)
class ResumePlan:
    target: ResumeTarget
    cwd: str
    argv: list[str]
    managed: bool

    def shell_line(self) -> str:
        return f"cd {shlex.quote(self.cwd)} && {shlex.join(self.argv)}"


@dataclass(frozen=True)
class ResumeCandidate:
    """One resumable session (a lane) and the open tasks it owns."""

    key: SessionKey
    task_numbers: tuple[str, ...]
    task_names: tuple[str, ...]
    monitor: bool
    last_timestamp: str | None
    last_message: str | None


@dataclass(frozen=True)
class RecentSession:
    """One conversation this project's chat log saw recently (`pb-session restore`)."""

    key: SessionKey
    last_activity: datetime
    lane: str | None
    task_number: str | None
    task_name: str | None
    task_status: str | None
    recent_messages: tuple[str, ...]  # oldest first; the last one is the latest
    recent_gates: tuple[str, ...]  # gates checked in `task_number`, oldest first
    running: bool = False
    folder: Path | None = None  # where the provider says it ran, when known


def _task_files(agent_dir: Path) -> Iterator[tuple[str, str, Path]]:
    tasks = agent_dir / "tasks"
    if tasks.is_symlink() or not tasks.is_dir():
        return
    for child in sorted(tasks.iterdir(), key=lambda path: path.name):
        prefix, separator, name = child.name.partition("-")
        if not separator or not prefix.isdigit() or child.is_symlink() or not child.is_dir():
            continue
        task_file = child / "task.md"
        if task_file.is_symlink() or not task_file.is_file():
            continue
        yield str(int(prefix)).zfill(3), name, task_file


def _parse(task_file: Path) -> TaskDocument:
    return TaskDocument.parse(task_file.read_text(encoding="utf-8"))


def _open_monitor(agent_dir: Path) -> ResumeTarget:
    boards: list[ResumeTarget] = []
    for number, name, task_file in _task_files(agent_dir):
        try:
            document = _parse(task_file)
            owner = document.live_owner if document.is_monitor_board else None
        except (OSError, TaskDocumentError):
            continue
        if owner is not None:
            boards.append(ResumeTarget(number, name, owner, True, MONITOR_SELECTOR))
    if not boards:
        raise ResumeError(
            "no open monitor task (an in-progress monitor board with an owner); "
            "resume by number with `pb-tasks resume <N>`"
        )
    if len(boards) > 1:
        listed = ", ".join(
            f"{board.task_number} {board.task_name} ({board.key.provider}:{board.key.session_id})"
            for board in boards
        )
        raise ResumeError(
            f"several open monitor tasks: {listed}; choose one with `pb-tasks resume <N>`"
        )
    return boards[0]


def resolve_target(agent_dir: Path, selector: str) -> ResumeTarget:
    """Map `monitor` or a task number to the session that last owned it."""
    if selector == MONITOR_SELECTOR:
        return _open_monitor(agent_dir)
    if not selector.isdigit():
        raise ResumeError(f"resume takes a task number or 'monitor', not {selector!r}")
    try:
        task_file = resolve_task_document(agent_dir, selector)
        document = _parse(task_file)
    except TaskAuthorityMismatch as exc:
        raise ResumeError(str(exc)) from exc
    except (OSError, TaskDocumentError) as exc:
        raise ResumeError(f"task {selector} is unreadable: {exc}") from exc
    number = str(int(selector)).zfill(3)
    if not document.sessions:
        raise ResumeError(f"task {number} has no recorded session in ## Sessions; nothing to resume")
    name = task_file.parent.name.partition("-")[2]
    # Done tasks keep their history, so their last session is still the one to resume.
    return ResumeTarget(number, name, document.sessions[-1], document.is_monitor_board, number)


def task_prompt(target: ResumeTarget) -> str:
    return (
        f"Resumed with `pb-tasks resume {target.via}` (task {target.task_number}), "
        "probably after a restart. Run `pb-tasks status`, then wait for the user."
    )


def monitor_prompt(target: ResumeTarget) -> str:
    number = target.task_number
    return (
        f"Resumed with `pb-tasks resume {target.via}` after a restart, probably a reboot. "
        f"You are the monitor for task {number}. Follow the monitor skill's recovery steps: "
        f"re-read task {number}, check every lane with `pb-session status`, resume lanes that "
        "were running within their approved scope, re-arm your watch, then report what you "
        "restored and what still needs the user."
    )


def transcript_problem(provider: str, session_id: str, cwd: str) -> str | None:
    """Say why the provider cannot resume this conversation, when we can tell."""
    if provider == "claude":
        from provider.adapters.claude import ClaudeAdapter

        if ClaudeAdapter(session_id, Path(cwd)).session_log_path() is None:
            return (
                f"Claude has no saved conversation {session_id} for {cwd} "
                "(looked in ~/.claude/projects). Claude deletes conversations older than "
                "`cleanupPeriodDays` (default 30 days)."
            )
    elif provider == "codex":
        from provider.adapters.codex import CodexAdapter

        if CodexAdapter.rollout_path_for(session_id) is None:
            return f"Codex has no saved rollout for thread {session_id} under ~/.codex/sessions."
    # Other providers keep no store we can check; their own resume reports failures.
    return None


def _session_record(agent_dir: Path, key: SessionKey) -> dict | None:
    import session_cli

    if not (session_directory(agent_dir, key) / "session.json").is_file():
        return None
    try:
        _path, record = session_cli.resolve_session_record(
            agent_dir, f"{key.provider}:{key.session_id}", include_destroyed=True
        )
    except SessionStateError as exc:
        raise ResumeError(str(exc)) from exc
    if record.get("state") == "destroyed":
        raise ResumeError(
            f"session {key.provider}:{key.session_id} was destroyed with `pb-session destroy`; "
            "it is kept for inspection, not resumed"
        )
    return record


def plan_resume(agent_dir: Path, project_root: Path, selector: str) -> ResumePlan:
    """Build the exact command that resumes the selected task's conversation."""
    import session_cli

    target = resolve_target(agent_dir, selector)
    key = target.key
    record = _session_record(agent_dir, key)
    prompt = monitor_prompt(target) if target.monitor else task_prompt(target)
    try:
        route = session_cli.native_resume_route(
            record,
            key.provider,
            key.session_id,
            fallback_cwd=project_root,
            prompt=prompt,
            attach=True,
        )
    except SessionStateError as exc:
        raise ResumeError(str(exc)) from exc
    assert route is not None  # a fallback cwd is always supplied
    cwd, argv = route
    if not Path(cwd).is_dir():
        # Do not fall back to another directory: Claude files conversations
        # by cwd, so it would look in the wrong place and mislead.
        raise ResumeError(
            f"cannot resume task {target.task_number}: the session's recorded directory "
            f"{cwd} no longer exists"
        )
    problem = transcript_problem(key.provider, key.session_id, cwd)
    if problem:
        raise ResumeError(f"cannot resume task {target.task_number}: {problem}")
    managed = bool(record and record.get("managed") is True)
    return ResumePlan(target, cwd, argv, managed)


def exec_plan(
    plan: ResumePlan, project_root: Path, environ: Mapping[str, str] = os.environ
) -> NoReturn:
    """Replace this process with the resumed agent, in its own cwd."""
    env = scrub_inherited_session_identity(environ)
    if not plan.managed:
        for name in _MANAGED_LAUNCH_ENV:
            env.pop(name, None)
        env["PLAYBOOK_PROVIDER"] = plan.target.key.provider
        env["PLAYBOOK_PROJECT_ROOT"] = str(project_root)
    os.chdir(plan.cwd)
    os.execvpe(plan.argv[0], plan.argv, env)


def _latest_human_messages(agent_dir: Path) -> dict[tuple[str, str], tuple[str, str]]:
    from tasks.chat_state import parse_chat_entries

    chat_log = agent_dir / "chat_log.md"
    if chat_log.is_symlink() or not chat_log.is_file():
        return {}
    latest: dict[tuple[str, str], tuple[str, str]] = {}
    for entry in parse_chat_entries(chat_log.read_text(encoding="utf-8")):
        if not (entry.marker.startswith("M") and entry.provider and entry.session_id):
            continue
        text = _display_message(entry.body)
        if not text or " ".join(entry.body.split()).startswith(_SYNTHETIC_MESSAGES):
            continue
        latest[(entry.provider, entry.session_id)] = (entry.timestamp, text)
    return latest


def list_candidates(agent_dir: Path) -> list[ResumeCandidate]:
    """Sessions owning open tasks, one per session, newest human message first.

    One session can own several tasks; listing it once keeps two tabs from
    resuming the same conversation.
    """
    owned: dict[SessionKey, list[tuple[str, str, bool]]] = {}
    for number, name, task_file in _task_files(agent_dir):
        try:
            document = _parse(task_file)
            owner = document.live_owner
        except (OSError, TaskDocumentError):
            continue
        if owner is not None:
            owned.setdefault(owner, []).append((number, name, document.is_monitor_board))
    latest = _latest_human_messages(agent_dir)
    candidates = [
        ResumeCandidate(
            owner,
            tuple(number for number, _, _ in tasks),
            tuple(name for _, name, _ in tasks),
            any(monitor for _, _, monitor in tasks),
            *latest.get((owner.provider, owner.session_id), (None, None)),
        )
        for owner, tasks in owned.items()
    ]
    candidates.sort(key=lambda candidate: candidate.last_timestamp or "", reverse=True)
    return candidates


def _clip(text: str, width: int = _MESSAGE_WIDTH) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def render_candidates(candidates: list[ResumeCandidate]) -> list[str]:
    if not candidates:
        return [
            "No open task has a recorded session to resume.",
            "`pb-session restore` lists every recent session here, with or without a task.",
        ]
    lines = ["Open tasks with a resumable session, newest activity first:", ""]
    for candidate in candidates:
        tag = " [monitor]" if candidate.monitor else ""
        when = candidate.last_timestamp or "no recorded message"
        lines.append(
            f"  {', '.join(candidate.task_numbers)}  {', '.join(candidate.task_names)}{tag}  "
            f"{candidate.key.provider}  {when}"
        )
        if candidate.last_message:
            lines.append(f"       {_clip(candidate.last_message)}")
    lines += [
        "",
        "Resume one with `pb-tasks resume <N>`, or `pb-tasks resume monitor` "
        "for the open monitor task.",
        "`pb-session restore` lists every recent session here, with or without a task.",
    ]
    return lines


# ── pb-session restore ───────────────────────────────────────────────────────


def _display_message(body: str) -> str:
    """The person's words without the wrappers their client added around them."""
    return " ".join(_TAG_RE.sub(" ", _IDE_CONTEXT_RE.sub(" ", body)).split())


def _entry_time(timestamp: str) -> datetime | None:
    text = timestamp.strip().removesuffix(" UTC")
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _keep_latest(seen: dict, field: str, stamp: tuple[datetime, int], value: object) -> None:
    # Writers supply their own timestamps, so log order is only the tie-break.
    if field not in seen or stamp > seen[field][0]:
        seen[field] = (stamp, value)


def list_recent_sessions(
    agent_dir: Path,
    *,
    now: datetime,
    days: float = RESTORE_DAYS,
    messages: int = RESTORE_MESSAGES,
    gates: int = RESTORE_GATES,
) -> list[RecentSession]:
    """Conversations active in this project's chat log within `days`, newest first.

    A session qualifies by something the person said or by task activity;
    sessions that only ever received generated prompts (test launches,
    bootstrap-only lanes) are left out.
    """
    from tasks.chat_state import _recorded_session_name, parse_chat_entries

    chat_log = agent_dir / "chat_log.md"
    if chat_log.is_symlink() or not chat_log.is_file():
        return []
    latest: dict[SessionKey, dict] = {}
    for order, entry in enumerate(parse_chat_entries(chat_log.read_text(encoding="utf-8"))):
        when = _entry_time(entry.timestamp)
        if not (entry.provider and entry.session_id) or when is None:
            continue
        try:
            key = SessionKey.from_values(entry.provider, entry.session_id)
        except (TypeError, ValueError):
            continue
        stamp = (when, order)
        seen = latest.setdefault(key, {})
        _keep_latest(seen, "activity", stamp, None)
        if entry.session_name:
            _keep_latest(seen, "lane", stamp, entry.session_name)
        task = _TASK_MARKER_RE.match(entry.marker)
        if task:
            number = str(int(task[1])).zfill(3)
            _keep_latest(seen, "task", stamp, number)
            if entry.marker.startswith("G"):
                gate = " ".join(_CHECKED_GATE_RE.sub("", entry.body.strip()).split())
                # Some closure rows carry no gate text (empty, or a `---` the
                # hook read from a shifted line); they say nothing about where you were.
                if any(character.isalnum() for character in gate):
                    seen.setdefault("gates", []).append((stamp, number, gate))
        if entry.marker.startswith("M"):
            raw = " ".join(entry.body.split())
            shown = _display_message(entry.body)
            if shown and not raw.startswith(_SYNTHETIC_MESSAGES):
                seen.setdefault("messages", []).append((stamp, shown))

    cutoff = now - timedelta(days=days)
    task_files = {number: (name, path) for number, name, path in _task_files(agent_dir)}
    found: list[tuple[tuple[datetime, int], RecentSession]] = []
    for key, seen in latest.items():
        stamp = seen["activity"][0]
        if stamp[0] < cutoff or not ("messages" in seen or "task" in seen):
            continue
        number = seen["task"][1] if "task" in seen else None
        said = sorted(seen.get("messages", []))[-messages:] if messages > 0 else []
        closed = sorted(gate for gate in seen.get("gates", []) if gate[1] == number)
        closed = closed[-gates:] if gates > 0 else []
        name = status = None
        if number in task_files:
            name, task_file = task_files[number]
            try:
                status = _parse(task_file).status
            except (OSError, TaskDocumentError):
                pass
        found.append((
            stamp,
            RecentSession(
                key=key,
                last_activity=stamp[0],
                # A lane named after its rows were logged (`pb-session rename`)
                # only carries the name in its session record.
                lane=seen["lane"][1] if "lane" in seen else _recorded_session_name(agent_dir, key),
                task_number=number,
                task_name=name,
                task_status=status,
                recent_messages=tuple(text for _stamp, text in said),
                recent_gates=tuple(text for _stamp, _number, text in closed),
            ),
        ))
    found.sort(key=lambda item: item[0], reverse=True)
    return [session for _stamp, session in found]


def _process_table() -> dict[int, tuple[int, str, str]]:
    """pid -> (parent pid, start time as `ps` prints it in UTC, command line).

    Empty when `ps` fails.
    """
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,lstart=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "TZ": "UTC"},
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    table: dict[int, tuple[int, str, str]] = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3 or not (fields[0].isdigit() and fields[1].isdigit()):
            continue
        rest = fields[2]
        if len(rest) >= 24:  # lstart is a fixed 24-column date
            table[int(fields[0])] = (int(fields[1]), " ".join(rest[:24].split()), rest[24:].strip())
    return table


def _restore_chain(table: Mapping[int, tuple[int, str, str]], pid: int) -> set[int]:
    """This process and the parents that launched it as `… restore <id>`.

    Their argv holds the ID being restored, so they must not count as that
    session running. The walk stops at the first parent that is not part of
    the restore command (a shell, or an agent that may really be the session).
    """
    chain = {pid}
    parent = table[pid][0] if pid in table else None
    while parent in table and parent not in chain and "restore" in table[parent][2].split():
        chain.add(parent)
        parent = table[parent][0]
    return chain


def running_session_keys(
    keys: Iterable[SessionKey],
    *,
    claude_sessions: Path | None = None,
    processes: Mapping[int, tuple[int, str, str]] | None = None,
    own_pid: int | None = None,
) -> set[SessionKey]:
    """Which of `keys` look alive on this machine right now.

    Advisory only (it decides whether to ask for `--force`, never which
    conversation to open): Claude's per-process files are undocumented, and a
    session started without its ID in argv is invisible to the argv check.
    """
    wanted = set(keys)
    table = _process_table() if processes is None else processes
    mine = _restore_chain(table, os.getpid() if own_pid is None else own_pid)
    folder = claude_sessions if claude_sessions is not None else Path.home() / ".claude" / "sessions"
    running: set[SessionKey] = set()
    try:
        pid_files = sorted(folder.glob("*.json"))
    except OSError:
        pid_files = []
    for pid_file in pid_files:
        try:
            record = json.loads(pid_file.read_text(encoding="utf-8"))
            key = SessionKey.from_values("claude", record["sessionId"])
            pid = record["pid"]
        except (OSError, ValueError, TypeError, KeyError):
            continue
        if key not in wanted or not isinstance(pid, int) or pid not in table:
            continue
        started = record.get("procStart")
        if isinstance(started, str) and " ".join(started.split()) != table[pid][1]:
            continue  # the pid was reused after the process that wrote this file died
        running.add(key)
    for pid, (_parent, _started, command) in table.items():
        if pid in mine:
            continue
        tokens = set(command.split())
        running.update(key for key in wanted if key.session_id in tokens)
    return running


def session_folder(provider: str, session_id: str) -> Path | None:
    """The folder the provider says this conversation ran in, when it keeps one.

    Claude files transcripts by folder and repeats `cwd` on its lines; Codex
    opens each rollout with a `session_meta` line carrying `cwd`. None when
    the transcript is gone or the provider keeps no store we can read.
    """
    if provider == "claude":
        candidates = sorted((Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
    elif provider == "codex":
        from provider.adapters.codex import CodexAdapter

        rollout = CodexAdapter.rollout_path_for(session_id)
        candidates = [rollout] if rollout else []
    else:
        return None
    for transcript in candidates:
        try:
            with transcript.open(encoding="utf-8") as lines:
                for _number, line in zip(range(200), lines):
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    payload = record.get("payload") if isinstance(record, dict) else None
                    cwd = record.get("cwd") if isinstance(record, dict) else None
                    if cwd is None and isinstance(payload, dict) and record.get("type") == "session_meta":
                        cwd = payload.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return Path(cwd)
        except OSError:
            continue
    return None


def _inside(folder: Path, root: Path) -> bool:
    """Whether `folder` is `root` or below it, robust to symlinks and letter case."""
    for candidate in (folder, *folder.parents):
        try:
            if os.path.samefile(candidate, root):
                return True
        except OSError:
            if candidate == root:
                return True
    return False


def plan_restore(agent_dir: Path, project_root: Path, session: RecentSession) -> ResumePlan:
    """The provider's own resume of `session`, in this terminal.

    Managed lanes come back here too, as plain sessions: their tmux body is
    gone after a reboot, and the person chose this terminal to continue in.
    """
    import session_cli

    key = session.key
    label = f"{key.provider}:{key.session_id}"
    record = _session_record(agent_dir, key)
    plain = {field: value for field, value in (record or {}).items() if field != "managed"}
    try:
        route = session_cli.native_resume_route(
            plain,
            key.provider,
            key.session_id,
            fallback_cwd=session.folder or project_root,
            prompt=RESTORE_PROMPT,
        )
    except (SessionStateError, KeyError, ValueError) as exc:
        raise ResumeError(f"cannot resume {label}: {exc}") from exc
    assert route is not None  # a fallback cwd is always supplied
    cwd, argv = route
    if not Path(cwd).is_dir():
        raise ResumeError(f"cannot resume {label}: its recorded directory {cwd} no longer exists")
    problem = transcript_problem(key.provider, key.session_id, cwd)
    if problem:
        raise ResumeError(f"cannot resume {label}: {problem}")
    target = ResumeTarget(
        session.task_number or "-", session.task_name or "", key, False, "restore"
    )
    return ResumePlan(target, cwd, argv, managed=False)


def _age(delta: timedelta) -> str:
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 48 * 3600:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def restore_handles(sessions: list[RecentSession]) -> list[str]:
    """What to type after `pb-session restore` for each row, in list order.

    The task number when the row has one (newest row per task), else the
    shortest session-ID prefix of at least 8 characters that no other listed
    session shares (Codex IDs start with a timestamp, so lanes launched
    together share their first 8). Neither shifts when other sessions get new
    activity, unlike a row number.
    """
    ids = [session.key.session_id for session in sessions]
    handles: list[str] = []
    claimed: set[str] = set()
    for session in sessions:
        if session.task_number and session.task_number not in claimed:
            claimed.add(session.task_number)
            handles.append(session.task_number)
            continue
        own = session.key.session_id
        length = 8
        while length < len(own) and any(
            other != own and other.startswith(own[:length]) for other in ids
        ):
            length += 1
        handles.append(own[:length])
    return handles


def render_recent_sessions(
    sessions: list[RecentSession],
    *,
    now: datetime,
    days: int,
    project: str,
    width: int = 100,
    detail: bool = True,
) -> list[str]:
    """One block per session: where it was (task, last gates) and what you last said.

    Every line fits in `width` columns, so a block never wraps.
    """
    indent = " " * 12
    handles = restore_handles(sessions)
    live = [handle for handle, session in zip(handles, sessions) if session.running]
    lines = [_clip(f"Sessions in {project} from the last {days} days, newest first:", width)]
    if live:
        lines.append(_clip(
            f"Running now: {', '.join(live)}. The rest are stopped and can be resumed here.",
            width,
        ))
    else:
        lines.append(_clip("None of these is running now; any of them can be resumed here.", width))
    lines.append("")
    for handle, session in zip(handles, sessions):
        parts = [
            f"  {handle:<8}",
            f"{_age(now - session.last_activity):>8}",
            f"{session.key.provider:<6}",
        ]
        if session.lane:
            parts.append(session.lane)
        if session.task_number:
            task = f"T{session.task_number}"
            if session.task_name:
                task += f" {session.task_name}"
            if session.task_status:
                task += f" ({session.task_status})"
            parts.append(task)
        if session.running:
            parts.append("[running]")
        lines.append(_clip("  ".join(parts).rstrip(), width))
        body = [f"✓ {gate}" for gate in session.recent_gates]
        body += [f"> {message}" for message in session.recent_messages]
        if detail:
            lines += [_clip(indent + line, width) for line in body or ["(no message)"]]
            lines.append("")
    if not detail:
        lines.append("")
    return lines


def pick_session(sessions: list[RecentSession], selector: str) -> RecentSession:
    """The row `selector` names: a task number (`188`, `T188`), a lane name, or an ID prefix."""
    task = selector[1:] if selector[:1] in ("T", "t") and selector[1:].isdigit() else selector
    if task.isdigit():
        for session in sessions:  # newest first, so the task's latest session wins
            if session.task_number and int(session.task_number) == int(task):
                return session
    for session in sessions:  # lane names get reused; the newest holder wins
        if session.lane == selector:
            return session
    matches = [session for session in sessions if session.key.session_id.startswith(selector)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ResumeError(f"{selector!r} matches {len(matches)} sessions, type more of the ID")
    if task.isdigit():
        raise ResumeError(
            f"no session worked on task {task} in this window "
            f"(`pb-tasks resume {int(task)}` reaches older tasks)"
        )
    raise ResumeError(f"no recent session ID starts with {selector!r}")


def run_restore(
    agent_dir: Path,
    project_root: Path,
    selector: str | None,
    *,
    now: datetime,
    write: Callable[[str], None],
    days: int = RESTORE_DAYS,
    force: bool = False,
    width: int = 100,
    messages: int = RESTORE_MESSAGES,
    gates: int = RESTORE_GATES,
    running: Callable[[Iterable[SessionKey]], set[SessionKey]] = running_session_keys,
    folder: Callable[[str, str], Path | None] = session_folder,
) -> ResumePlan | None:
    """Without a selector, print this project's recent sessions; with one, plan its resume."""
    print_line = write

    def write(line: str) -> None:  # every report line fits in `width` columns
        print_line(_clip(line, width))

    listed = list_recent_sessions(agent_dir, now=now, days=days, messages=messages, gates=gates)
    # The chat log can hold sessions that ran elsewhere (a hook pointed at this
    # project from another folder); only this project's own are offered.
    located = [
        replace(session, folder=folder(session.key.provider, session.key.session_id))
        for session in listed
    ]
    sessions = [s for s in located if s.folder is None or _inside(s.folder, project_root)]
    elsewhere = [s for s in located if s not in sessions]
    if not sessions:
        write(f"No sessions in {project_root.name} from the last {days} days.")
        return None
    live = running(session.key for session in sessions)
    sessions = [replace(session, running=session.key in live) for session in sessions]
    picked = None
    if selector is not None:
        try:
            picked = pick_session(sessions, selector)
        except ResumeError as exc:
            write(f"Nothing resumed: {exc}.")
            write("")
    if picked is None:
        for line in render_recent_sessions(
            sessions,
            now=now,
            days=days,
            project=project_root.name,
            width=width,
            detail=messages > 0 or gates > 0,
        ):
            write(line)
        for other in elsewhere:
            home = str(Path.home())
            where = str(other.folder)
            where = "~" + where[len(home):] if where.startswith(home + os.sep) else where
            write(
                f"Left out {other.key.provider} {other.key.session_id[:8]}: logged here, "
                f"but it ran in {where}."
            )
        write("Resume one here with `pb-session restore <task number, lane name or ID>`.")
        return None
    label = f"{picked.key.provider}:{picked.key.session_id}"
    if picked.running and not force:
        raise ResumeError(
            f"{label} looks open in another terminal; add --force to resume it here too"
        )
    return plan_restore(agent_dir, project_root, picked)
