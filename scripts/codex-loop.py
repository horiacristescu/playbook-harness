"""Send a recurring prompt to one open Codex session without tmux.

A loop is armed against one session ID and lives exactly as long as the Codex
window showing that session. Codex can keep a thread loaded in its shared
background server after the window closes and answer queued prompts with
nobody watching, so the loop finds the session's own window at start and
stops when that window exits. A window started with `--no-daemon` (as
`pb-codex` does) runs its session in-process, so the arming command's parent
chain names it exactly; a window resumed with the session ID in its arguments
is the only other exact match.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


INTERVAL = re.compile(r"^([1-9][0-9]*)([smhd])$")
SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
THREAD_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}$")
UUID_TOKEN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Codex subcommands that never host an interactive session.
NOT_A_WINDOW = {
    "a", "app", "app-server", "apply", "cloud", "completion", "debug", "e", "exec",
    "features", "help", "login", "logout", "mcp", "mcp-server", "proxy", "queue",
    "review", "sandbox",
}
OWNER_RECHECK_SECONDS = 10
SANDBOXED = (
    "pb-codex loop must start outside the Codex sandbox: its background process "
    "calls `codex queue`, which the sandbox blocks. Run the same command again "
    "with escalated permissions."
)


NO_WINDOW = (
    "cannot tell which Codex window shows session {thread}. A window that "
    "shares Codex's background server does not reveal its session. Quit that "
    "window, run `pb-codex fork {thread}` to continue the conversation in a "
    "window the loop can follow, and arm the loop again there (`pb-codex resume` "
    "is refused for about 30 minutes, while the server still holds the session). "
    "Start monitors with `pb-codex` (or `codex --no-daemon`) to avoid this. Or "
    "pass --owner <pid of that window's codex process>."
)


def state_root() -> Path:
    override = os.environ.get("PLAYBOOK_CODEX_LOOP_DIR")
    root = Path(override) if override else Path(tempfile.gettempdir()) / f"playbook-codex-loops-{os.getuid()}"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def write_state(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_interval(value: str) -> int:
    match = INTERVAL.fullmatch(value)
    if not match:
        raise argparse.ArgumentTypeError("interval must be a positive number followed by s, m, h, or d (for example 5m)")
    return int(match.group(1)) * SECONDS[match.group(2)]


def resolve_thread(explicit: str | None) -> str:
    inherited = os.environ.get("CODEX_THREAD_ID")
    normalized = os.environ.get("PLAYBOOK_SESSION_ID", "")
    thread = explicit or inherited or (normalized.removeprefix("codex:") if normalized.startswith("codex:") else "")
    if not THREAD_ID.fullmatch(thread):
        raise ValueError("Codex thread ID missing or invalid; pass --thread <UUID> outside the target Codex session")
    return thread


def sandboxed() -> bool:
    # Escalated commands drop CODEX_SANDBOX but keep CODEX_SANDBOX_NETWORK_DISABLED.
    return bool(os.environ.get("CODEX_SANDBOX"))


def process_started(pid: int) -> str | None:
    """Return the process start time, which tells a live owner from a reused PID."""
    try:
        result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def process_row(pid: int) -> dict | None:
    """Return one process's parent, terminal, executable, and command line."""
    try:
        head = subprocess.run(["ps", "-o", "ppid=,tty=,comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False)
        argv = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    parts = head.stdout.strip().split(None, 2)
    if len(parts) != 3:
        return None
    return {"pid": pid, "ppid": int(parts[0]), "tty": parts[1], "comm": parts[2], "command": argv.stdout.strip()}


def is_window(row: dict) -> bool:
    """A Codex window is an interactive `codex` process attached to a terminal."""
    if row["tty"] in {"??", "?", "-"} or Path(row["comm"]).name != "codex":
        return False
    words = row["command"].split()[1:]
    first = next((word for word in words if not word.startswith("-")), None)
    return first not in NOT_A_WINDOW


def window_in_ancestry(pid: int, read=process_row) -> int | None:
    """Return the Codex window among `pid` and its ancestors, if any."""
    seen = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        row = read(pid)
        if row is None:
            return None
        if is_window(row):
            return pid
        pid = row["ppid"]
    return None


def process_table() -> list[dict]:
    """List every process running an executable named `codex`."""
    result = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True, timeout=10, check=True)
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and Path(parts[1]).name == "codex":
            row = process_row(int(parts[0]))
            if row is not None:
                rows.append(row)
    return rows


def resumed_windows(thread: str, rows: list[dict]) -> list[int]:
    """Return the windows whose own arguments name this session ID."""
    return [
        row["pid"] for row in rows
        if is_window(row) and thread.lower() in {token.lower() for token in UUID_TOKEN.findall(row["command"])}
    ]


def session_window(thread: str) -> list[int]:
    """Find the window showing `thread`, matching only on the session ID."""
    if os.environ.get("CODEX_THREAD_ID", "").lower() == thread.lower():
        window = window_in_ancestry(os.getppid())
        if window is not None:
            return [window]
    return resumed_windows(thread, process_table())


def owners_alive(owners: list[dict], recheck: bool) -> bool:
    alive = []
    for owner in owners:
        try:
            os.kill(owner["pid"], 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        if recheck and process_started(owner["pid"]) not in (None, owner["started"]):
            continue  # the PID now belongs to another process
        alive.append(owner)
    owners[:] = alive
    return bool(alive)


def wait_until(deadline: float, stop: Path, path: Path, state: dict) -> bool:
    last_heartbeat = 0.0
    while True:
        if stop.exists():
            state["stop_reason"] = "stop requested"
            return False
        now = time.monotonic()
        recheck = now - last_heartbeat >= OWNER_RECHECK_SECONDS
        if not owners_alive(state["owners"], recheck):
            state["stop_reason"] = "Codex window closed"
            return False
        if recheck:
            state["heartbeat_at"] = time.time()
            write_state(path, state)
            last_heartbeat = now
        remaining = deadline - now
        if remaining <= 0:
            return True
        time.sleep(min(remaining, 1.0))


def worker(loop_id: str) -> int:
    root = state_root()
    path = root / f"{loop_id}.json"
    stop = root / f"{loop_id}.stop"
    state = read_state(path)
    state.update(pid=os.getpid(), status="running")
    write_state(path, state)
    interval = state["interval_seconds"]
    deadline = time.monotonic() + interval
    while wait_until(deadline, stop, path, state):
        try:
            result = subprocess.run(
                ["codex", "queue", "--thread", state["thread"], "--message", state["message"]],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode == 0:
                state["sent_count"] += 1
                state["last_sent_at"] = time.time()
                state["last_error"] = None
            else:
                state["last_error"] = (result.stderr or result.stdout).strip()[:500] or f"codex queue exited {result.returncode}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            state["last_error"] = str(exc)[:500]
        state["last_attempt_at"] = time.time()
        write_state(path, state)
        deadline = time.monotonic() + interval
    state["status"] = "stopped"
    state["stopped_at"] = time.time()
    write_state(path, state)
    stop.unlink(missing_ok=True)
    return 0


def running_loops(root: Path) -> list[dict]:
    loops = []
    for path in sorted(root.glob("*.json")):
        try:
            state = read_state(path)
        except (OSError, ValueError):
            continue
        if state.get("status") != "stopped":
            loops.append(state)
    return loops


def start(interval: int, interval_text: str, message: str, thread: str, owner_pids: list[int]) -> int:
    if not message.strip():
        raise ValueError("reminder text cannot be empty")
    if sandboxed():
        raise RuntimeError(SANDBOXED)
    if not owner_pids:
        owner_pids = session_window(thread)
    owners = [{"pid": pid, "started": process_started(pid)} for pid in owner_pids]
    owners = [owner for owner in owners if owner["started"]]
    if not owners:
        raise ValueError(NO_WINDOW.format(thread=thread))
    root = state_root()
    for previous in running_loops(root):
        if previous.get("thread") == thread:
            (root / f"{previous['id']}.stop").touch()
            print(f"Replaced Codex loop {previous['id']} on the same thread.")
    loop_id = uuid.uuid4().hex[:12]
    path = root / f"{loop_id}.json"
    state = {
        "id": loop_id,
        "thread": thread,
        "interval": interval_text,
        "interval_seconds": interval,
        "message": message,
        "owners": owners,
        "status": "starting",
        "pid": None,
        "sent_count": 0,
        "last_sent_at": None,
        "last_attempt_at": None,
        "last_error": None,
        "stop_reason": None,
        "started_at": time.time(),
        "heartbeat_at": None,
    }
    write_state(path, state)
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "_run", loop_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        path.unlink(missing_ok=True)
        raise
    for _ in range(60):
        time.sleep(0.05)
        if read_state(path)["status"] != "starting":
            pids = ", ".join(str(owner["pid"]) for owner in owners)
            print(f"Started Codex loop {loop_id}: every {interval_text} to {thread}")
            print(f"It stops when Codex window process {pids} exits.")
            return 0
    (root / f"{loop_id}.stop").touch()
    raise RuntimeError(f"loop {loop_id} did not start; inspect {path}")


def list_loops() -> int:
    found = False
    for state in running_loops(state_root()):
        found = True
        status = state["status"]
        heartbeat = state.get("heartbeat_at")
        if status == "running" and (heartbeat is None or time.time() - heartbeat > 45):
            status = "unresponsive"
        detail = f"{state['sent_count']} sent"
        if state.get("last_error"):
            detail += f"; last error: {state['last_error']}"
        print(f"{state['id']}  {status}  every {state['interval']}  {state['thread']}  {detail}  {state['message']}")
    if not found:
        print("No active Codex loops.")
    return 0


def stop_loop(loop_id: str) -> int:
    root = state_root()
    path = root / f"{loop_id}.json"
    if not re.fullmatch(r"[0-9a-f]{12}", loop_id) or not path.is_file():
        raise ValueError(f"unknown loop: {loop_id}")
    state = read_state(path)
    if state.get("status") == "stopped":
        print(f"Codex loop {loop_id} is already stopped ({state.get('stop_reason') or 'stopped'}).")
        return 0
    (root / f"{loop_id}.stop").touch()
    print(f"Stop requested for Codex loop {loop_id}.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pb-codex loop", description="Queue a recurring reminder to one open Codex session.")
    parser.add_argument("interval", nargs="?", help="interval such as 5m; or list / stop")
    parser.add_argument("words", nargs="*", help="reminder text")
    parser.add_argument("--thread", help="target Codex thread UUID; defaults to this session")
    parser.add_argument("--owner", type=int, action="append", default=[], help="Codex window PID the loop lives with; found automatically")
    args = parser.parse_args(argv)
    if args.interval == "_run":
        if len(args.words) != 1:
            parser.error("internal worker requires a loop ID")
        return worker(args.words[0])
    if args.interval == "list":
        if args.words:
            parser.error("list takes no arguments")
        return list_loops()
    if args.interval == "stop":
        if len(args.words) != 1:
            parser.error("stop requires one loop ID")
        return stop_loop(args.words[0])
    if args.interval is None or not args.words:
        parser.error("usage: pb-codex loop <interval> <message...> [--thread UUID]")
    try:
        interval = parse_interval(args.interval)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    return start(interval, args.interval, " ".join(args.words), resolve_thread(args.thread), args.owner)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"pb-codex loop: {exc}", file=sys.stderr)
        raise SystemExit(1)
