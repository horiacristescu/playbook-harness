"""Resume a task's agent conversation by task number.

After a reboot the task number is the handle a person remembers, and task.md
already names the provider-native session that owns the work (`## Sessions`,
latest entry last). Everything here is derived from task.md, session records,
chat_log.md and the provider's own transcript store; nothing new is persisted.

Lanes are tmux boxes owned by a monitor, so `resume monitor` brings back only
the monitor. Its prompt tells it to recover its lanes from its own board.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, NoReturn

from provider.session_identity import scrub_inherited_session_identity
from provider.session_state import SessionKey, SessionStateError, session_directory
from tasks.task_document import (
    TaskAuthorityMismatch,
    TaskDocument,
    TaskDocumentError,
    resolve_task_document,
)

MONITOR_SELECTOR = "monitor"
_MESSAGE_WIDTH = 96
# Rows captured as M entries that the person did not type: harness injections
# and the first prompts that resume/launch commands hand to the agent.
_SYNTHETIC_MESSAGES = (
    "<",
    "[Request interrupted by user]",
    "Resumed with `pb-tasks resume",
    "Run `pb-tasks bootstrap`, then wait.",
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
        text = " ".join(entry.body.split())
        if not text or text.startswith(_SYNTHETIC_MESSAGES):
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
        return ["No open task has a recorded session to resume."]
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
    ]
    return lines
