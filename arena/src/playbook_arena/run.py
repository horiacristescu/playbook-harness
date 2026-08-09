"""One-run coordinator using the public pb-tmux-agent JSON contract."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .case import ArenaCaseError, CaseDefinition
from .controller import decide, record_decision, record_send_failure, record_sent, replay_controller
from .evidence import run_deterministic_checks
from .judge import aggregate_judgments, run_judges
from .prepare import MAX_WORKSPACE_FILE_BYTES, MAX_WORKSPACE_TOTAL_BYTES, prepare_case
from .schema import CampaignDefinition, RubricDefinition, ScriptDefinition, VariantDefinition, canonical_json
from .store import RunStore
from .variant import prepare_variant_runtime


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(list(argv), text=True, capture_output=True, check=False)
    except OSError as exc:
        raise ArenaCaseError(f"could not execute {argv[0]}: {exc}") from exc


class TmuxTransport:
    def __init__(self, *, command: str = "pb-tmux-agent", runner: CommandRunner = _default_runner) -> None:
        self.command = command
        self.runner = runner

    def _json(self, arguments: list[str], *, accepted: set[int] = {0}) -> dict[str, Any]:
        command = list(arguments)
        if "--" in command:
            command.insert(command.index("--"), "--json")
        else:
            command.append("--json")
        result = self.runner([self.command, *command])
        if result.returncode not in accepted:
            raise ArenaCaseError(f"pb-tmux-agent {' '.join(arguments[:2])} failed: {result.stderr.strip() or result.stdout.strip()}")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ArenaCaseError("pb-tmux-agent returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ArenaCaseError("pb-tmux-agent returned a non-object")
        return value

    def start(self, *, name: str, namespace: str, state_dir: Path, cwd: Path, agent: str, args: Sequence[str], model: str | None, environment: Mapping[str, str]) -> dict[str, Any]:
        command = ["start", name, agent, "--namespace", namespace, "--state-dir", str(state_dir), "--cwd", str(cwd)]
        for key, value in sorted(environment.items()):
            command.extend(["--env", f"{key}={value}"])
        if model is not None:
            command.extend(["--model", model])
        command.extend(["--", *args])
        return self._json(command)

    def send(self, *, name: str, namespace: str, state_dir: Path, message: str) -> None:
        self._json(["send", name, "--namespace", namespace, "--state-dir", str(state_dir), message])

    def status(self, *, name: str, namespace: str, state_dir: Path) -> dict[str, Any]:
        return self._json(["status", name, "--namespace", namespace, "--state-dir", str(state_dir)])

    def log(self, *, name: str, namespace: str, state_dir: Path) -> str:
        value = self._json(["log", name, "--namespace", namespace, "--state-dir", str(state_dir)])
        log = value.get("log")
        if not isinstance(log, str):
            raise ArenaCaseError("pb-tmux-agent log response lacks text")
        return log

    def result(self, *, name: str, namespace: str, state_dir: Path) -> dict[str, Any]:
        return self._json(["result", name, "--namespace", namespace, "--state-dir", str(state_dir)])

    def stop(self, *, name: str, namespace: str, state_dir: Path) -> dict[str, Any]:
        return self._json(["stop", name, "--namespace", namespace, "--state-dir", str(state_dir)])


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    run_root: Path
    state: str
    worker_state: str


def _expanded(argv: Sequence[str], *, workspace: Path, runtime: Path, packet: Path) -> list[str]:
    replacements = {"{workspace}": str(workspace), "{runtime}": str(runtime), "{packet}": str(packet)}
    return [replacements.get(value, value) for value in argv]


def _workspace_manifest(root: Path) -> list[dict[str, Any]]:
    values = []
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ArenaCaseError(f"worker workspace contains a link: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ArenaCaseError(f"worker workspace contains a special file: {relative}")
        size = path.stat().st_size
        if size > MAX_WORKSPACE_FILE_BYTES:
            raise ArenaCaseError(f"worker workspace file is too large: {relative}")
        total += size
        if len(values) >= 10_000 or total > MAX_WORKSPACE_TOTAL_BYTES:
            raise ArenaCaseError("worker workspace exceeds file-count or total-byte limit")
        data = path.read_bytes()
        if len(data) != size:
            raise ArenaCaseError(f"worker workspace changed during evidence capture: {relative}")
        values.append({"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "executable": bool(path.stat().st_mode & 0o111)})
    return values


def _write_workspace_evidence(store: RunStore, *, private: Path, baseline: Path, workspace: Path, output_limit: int) -> None:
    before = _workspace_manifest(baseline)
    after = _workspace_manifest(workspace)
    status_path = private / "workspace-status.json"
    status_path.write_bytes(canonical_json({"schema": 1, "before": before, "after": after}) + b"\n")
    store.record_artifact(status_path, "worker/workspace-status.json", kind="workspace", state="collecting")
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "diff", "--no-index", "--binary", "--no-ext-diff", "--", str(baseline), str(workspace)], capture_output=True, check=False)
    if result.returncode not in {0, 1}:
        raise ArenaCaseError(f"workspace diff failed: {result.stderr.decode('utf-8', errors='replace').strip()}")
    diff_path = private / "workspace.diff"
    if len(result.stdout) <= output_limit:
        diff_path.write_bytes(result.stdout)
        kind = "diff"
    else:
        diff_path.write_bytes(canonical_json({"schema": 1, "status": "omitted", "reason": "diff exceeds frozen output limit", "bytes": len(result.stdout), "sha256": hashlib.sha256(result.stdout).hexdigest()}) + b"\n")
        kind = "diff-missingness"
    store.record_artifact(diff_path, "worker/workspace.diff", kind=kind, state="collecting")
    selected = [workspace / ".agent/chat_log.md"]
    selected.extend(sorted(workspace.glob(".agent/tasks/*/task.md")))
    for path in selected:
        if path.exists() or path.is_symlink():
            relative = path.relative_to(workspace).as_posix()
            store.record_artifact(path, f"worker/project/{relative}", kind="project-state", state="collecting")


def run_identity(campaign: CampaignDefinition, variant: VariantDefinition, *, arm_id: str, repetition: int) -> tuple[str, dict[str, Any]]:
    identity = {"campaign_id": campaign.id, "campaign_sha256": campaign.sha256, "variant_sha256": variant.sha256, "arm_id": arm_id, "repetition": repetition}
    digest = hashlib.sha256(canonical_json(identity)).hexdigest()
    return f"{campaign.id}-{digest[:16]}", {**identity, "identity_sha256": digest}


@dataclass(frozen=True)
class RecoveryPaths:
    private: Path
    workspace: Path
    baseline: Path
    runtime: Path
    state_dir: Path
    packet: Path
    check_workspace: Path
    check_evidence: Path


def _recovery_paths(private: Path) -> RecoveryPaths:
    return RecoveryPaths(private, private / "workspace", private / "baseline", private / "runtime", private / "tmux", private / "worker-packet.json", private / "check-workspace", private / "check-evidence")


def _write_recovery(store: RunStore, *, run_id: str, identity: Mapping[str, Any], private: Path, control_deadline_epoch: float) -> None:
    value = {"schema": 1, "run_id": run_id, "identity_sha256": identity["identity_sha256"], "private": str(private.resolve()), "started_epoch": time.time(), "control_deadline_epoch": control_deadline_epoch}
    encoded = canonical_json(value) + b"\n"
    path = store.root / "recovery.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
        offset = 0
        while offset < len(encoded):
            written = os.write(fd, encoded[offset:])
            if written <= 0:
                raise ArenaCaseError("short write while recording recovery identity")
            offset += written
        os.fsync(fd)
    finally:
        if "fd" in locals():
            os.close(fd)
    store.append("recovery_recorded", {"sha256": hashlib.sha256(encoded).hexdigest()}, state="preparing")


def _load_recovery(store: RunStore, *, run_id: str, identity: Mapping[str, Any]) -> tuple[RecoveryPaths, dict[str, Any]]:
    path = store.root / "recovery.json"
    if path.is_symlink() or not path.is_file():
        raise ArenaCaseError("run recovery identity is missing or not regular")
    data = path.read_bytes()
    records = [event.payload.get("sha256") for event in store.verify() if event.type == "recovery_recorded"]
    if records != [hashlib.sha256(data).hexdigest()]:
        raise ArenaCaseError("run recovery identity hash mismatch")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ArenaCaseError(f"run recovery identity is invalid: {exc}") from exc
    if not isinstance(value, dict) or set(value) != {"schema", "run_id", "identity_sha256", "private", "started_epoch", "control_deadline_epoch"} or value["schema"] != 1 or value["run_id"] != run_id or value["identity_sha256"] != identity["identity_sha256"]:
        raise ArenaCaseError("run recovery identity does not match frozen assignment")
    if not isinstance(value["started_epoch"], (int, float)) or not isinstance(value["control_deadline_epoch"], (int, float)):
        raise ArenaCaseError("run recovery deadlines are invalid")
    private = Path(value["private"])
    if private.is_symlink() or not private.is_dir() or private.resolve() != private:
        raise ArenaCaseError("run private recovery root is unavailable")
    paths = _recovery_paths(private)
    for required in (paths.workspace, paths.baseline, paths.runtime, paths.state_dir, paths.packet):
        if required.is_symlink() or not required.exists():
            raise ArenaCaseError(f"run recovery component is unavailable: {required.name}")
    return paths, value


def _control_and_collect(
    *,
    store: RunStore,
    campaign: CampaignDefinition,
    script: ScriptDefinition,
    rubric: RubricDefinition,
    run_id: str,
    paths: RecoveryPaths,
    transport: TmuxTransport,
    control_deadline_epoch: float,
    started_epoch: float,
    poll_seconds: float,
) -> RunOutcome:
    namespace = campaign.id
    worker_name = hashlib.sha256(run_id.encode()).hexdigest()[:16]
    worker_state = "unknown"
    prior_log = ""
    while True:
        status = transport.status(name=worker_name, namespace=namespace, state_dir=paths.state_dir)
        worker_state = str(status.get("state", "unknown"))
        current_log = transport.log(name=worker_name, namespace=namespace, state_dir=paths.state_dir)
        if not current_log.startswith(prior_log):
            raise ArenaCaseError("tmux terminal log is not append-only")
        if len(current_log.encode("utf-8")) > campaign.limits["max_output_bytes"]:
            raise ArenaCaseError("worker output exceeded frozen limit")
        controller = replay_controller(script, store.verify())
        new_output = current_log[controller.output_offset:] if controller.delivery_sent else ""
        timed_out = controller.acknowledgment_deadline_epoch is not None and time.time() >= controller.acknowledgment_deadline_epoch
        decision = decide(script, controller, worker_state=worker_state, new_output=new_output, timed_out=timed_out)
        if decision.action == "deliver":
            if sum(event.type == "delivery_requested" for event in store.verify()) >= campaign.limits["max_interventions"]:
                raise ArenaCaseError("controller intervention limit reached")
            record_decision(store, script, decision)
            output_offset = len(current_log)
            try:
                transport.send(name=worker_name, namespace=namespace, state_dir=paths.state_dir, message=script.events[controller.next_index].message)
            except Exception as exc:
                record_send_failure(store, script, str(decision.event_id), str(exc))
                raise
            deadline = time.time() + script.events[controller.next_index].timeout_seconds
            record_sent(store, script, str(decision.event_id), output_offset=output_offset, acknowledgment_deadline_epoch=deadline)
        elif decision.action in {"acknowledge", "skip", "stop", "fail"}:
            record_decision(store, script, decision)
            if decision.action in {"stop", "fail"}:
                break
        elif decision.action == "complete" and worker_state in {"completed", "failed", "stopped", "lost"}:
            break
        if time.time() >= control_deadline_epoch:
            store.append("wall_timeout", {"wall_seconds": campaign.limits["wall_seconds"]}, state="stopping")
            break
        prior_log = current_log
        time.sleep(poll_seconds)
    status = transport.status(name=worker_name, namespace=namespace, state_dir=paths.state_dir)
    worker_state = str(status.get("state", "unknown"))
    if worker_state not in {"completed", "failed", "stopped", "lost"}:
        transport.stop(name=worker_name, namespace=namespace, state_dir=paths.state_dir)
        worker_state = str(transport.status(name=worker_name, namespace=namespace, state_dir=paths.state_dir).get("state", "unknown"))
    terminal = transport.log(name=worker_name, namespace=namespace, state_dir=paths.state_dir)
    terminal_path = paths.private / "terminal.log"
    terminal_path.write_text(terminal, encoding="utf-8")
    store.record_artifact(terminal_path, "worker/terminal.log", kind="terminal", state="collecting")
    result = transport.result(name=worker_name, namespace=namespace, state_dir=paths.state_dir) if worker_state != "lost" else status
    result_path = paths.private / "tmux-result.json"
    result_path.write_bytes(canonical_json(result) + b"\n")
    store.record_artifact(result_path, "worker/tmux-result.json", kind="transport", state="collecting")
    _write_workspace_evidence(store, private=paths.private, baseline=paths.baseline, workspace=paths.workspace, output_limit=campaign.limits["max_output_bytes"])
    if worker_state != "lost":
        transport.stop(name=worker_name, namespace=namespace, state_dir=paths.state_dir)
    shutil.copytree(paths.workspace, paths.check_workspace, symlinks=True)
    paths.check_evidence.mkdir()
    check_results = run_deterministic_checks(store, campaign, workspace=paths.check_workspace, runtime=paths.runtime, packet=paths.packet, evidence_dir=paths.check_evidence)
    judgments = run_judges(store, campaign, rubric, opaque_run_id=run_id, runtime=paths.runtime)
    judgment_summary = aggregate_judgments(rubric, judgments)
    judgment_path = paths.private / "judgment-summary.json"
    judgment_path.write_bytes(canonical_json({"schema": 1, "criteria": judgment_summary}) + b"\n")
    store.record_artifact(judgment_path, "judges/summary.json", kind="judgment-summary", state="collecting")
    final_state = "completed" if worker_state == "completed" and replay_controller(script, store.verify()).next_index == len(script.events) else "failed"
    final_events = store.verify()
    store.append("run_finished", {"worker_state": worker_state, "controller_state": replay_controller(script, final_events).terminal, "checks_passed": sum(bool(item["passed"]) for item in check_results), "checks_failed": sum(not bool(item["passed"]) for item in check_results), "judges_scored": sum(item["status"] == "scored" for item in judgments), "judges_unscorable": sum(item["status"] != "scored" for item in judgments), "interventions": sum(event.type == "delivery_requested" for event in final_events), "deviations": sum(event.type in {"script_event_skipped", "controller_failed", "controller_stopped"} for event in final_events), "duration_ms": round((time.time() - started_epoch) * 1000)}, state=final_state)
    store.verify_artifacts()
    return RunOutcome(run_id, store.root, final_state, worker_state)


def run_worker(
    *,
    campaign: CampaignDefinition,
    case: CaseDefinition,
    script: ScriptDefinition,
    rubric: RubricDefinition,
    variant: VariantDefinition,
    arm_id: str,
    repetition: int,
    results_root: str | Path,
    sources: Mapping[str, Path],
    runtime_repo: str | Path,
    corpus: Path | None = None,
    transport: TmuxTransport | None = None,
    poll_seconds: float = 0.05,
) -> RunOutcome:
    if campaign.case_id != case.id:
        raise ArenaCaseError("campaign case does not match selected case")
    run_id, identity = run_identity(campaign, variant, arm_id=arm_id, repetition=repetition)
    tmux = TmuxTransport() if transport is None else transport
    results = Path(results_root).expanduser()
    results.mkdir(parents=True, mode=0o700, exist_ok=True)
    private = Path(tempfile.mkdtemp(prefix="pb-arena-run-")).resolve()
    paths = _recovery_paths(private)
    namespace = campaign.id
    worker_name = hashlib.sha256(run_id.encode()).hexdigest()[:16]
    started = False
    worker_state = "not_started"
    store: RunStore | None = None
    control_deadline_epoch = time.time() + campaign.limits["wall_seconds"]
    try:
        store = RunStore.reserve(results, run_id, identity)
        _write_recovery(store, run_id=run_id, identity=identity, private=private, control_deadline_epoch=control_deadline_epoch)
        recovery = json.loads((store.root / "recovery.json").read_text(encoding="utf-8"))
        prepared = prepare_case(case, paths.workspace, sources=sources, corpus=corpus)
        store.append("case_prepared", prepared, state="preparing")
        shutil.copytree(paths.workspace, paths.baseline, symlinks=True)
        runtime_result = prepare_variant_runtime(variant, runtime_repo, paths.runtime)
        store.append("variant_prepared", {**runtime_result, "arm_id": arm_id}, state="preparing")
        paths.packet.write_bytes(canonical_json({"schema": 1, "case_id": case.id}) + b"\n")
        arguments = _expanded(campaign.worker_args, workspace=paths.workspace, runtime=paths.runtime, packet=paths.packet)
        path = f"{paths.runtime / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
        coordinator_src = Path(__file__).resolve().parents[1]
        python_path = os.pathsep.join((str(coordinator_src), str(paths.runtime / "arena/src"), str(paths.runtime / "src")))
        metadata = tmux.start(name=worker_name, namespace=namespace, state_dir=paths.state_dir, cwd=paths.workspace, agent=campaign.worker_agent, args=arguments, model=campaign.worker_model, environment={"PLAYBOOK_RUNTIME_DIR": str(paths.runtime), "PLAYBOOK_ARENA_PACKET": str(paths.packet), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": python_path, "PATH": path})
        started = True
        store.append("worker_started", {"transport": {key: metadata.get(key) for key in ("namespace", "name", "tmux_session", "agent", "model", "command")}}, state="controlling")
        return _control_and_collect(store=store, campaign=campaign, script=script, rubric=rubric, run_id=run_id, paths=paths, transport=tmux, control_deadline_epoch=control_deadline_epoch, started_epoch=recovery["started_epoch"], poll_seconds=poll_seconds)
    except Exception as exc:
        if store is not None:
            try:
                store.append("run_error", {"error": str(exc), "worker_state": worker_state}, state="failed")
            except Exception:
                pass
        raise
    finally:
        if started:
            try:
                status = tmux.status(name=worker_name, namespace=namespace, state_dir=paths.state_dir)
                if status.get("state") != "lost":
                    tmux.stop(name=worker_name, namespace=namespace, state_dir=paths.state_dir)
            except Exception:
                pass
        if store is not None:
            store.close()
        shutil.rmtree(private, ignore_errors=True)


def resume_worker(
    *,
    campaign: CampaignDefinition,
    script: ScriptDefinition,
    rubric: RubricDefinition,
    variant: VariantDefinition,
    arm_id: str,
    repetition: int,
    results_root: str | Path,
    transport: TmuxTransport | None = None,
    poll_seconds: float = 0.05,
) -> RunOutcome:
    run_id, identity = run_identity(campaign, variant, arm_id=arm_id, repetition=repetition)
    store = RunStore.resume(Path(results_root) / run_id)
    tmux = TmuxTransport() if transport is None else transport
    paths: RecoveryPaths | None = None
    try:
        events = store.verify()
        if any(event.type in {"run_error", "artifact_recorded", "check_started", "judge_started", "run_finished"} for event in events):
            raise ArenaCaseError("run is not in the resumable control phase")
        paths, recovery = _load_recovery(store, run_id=run_id, identity=identity)
        return _control_and_collect(store=store, campaign=campaign, script=script, rubric=rubric, run_id=run_id, paths=paths, transport=tmux, control_deadline_epoch=recovery["control_deadline_epoch"], started_epoch=recovery["started_epoch"], poll_seconds=poll_seconds)
    except Exception as exc:
        try:
            store.append("run_error", {"error": str(exc), "worker_state": "recovery"}, state="failed")
        except Exception:
            pass
        raise
    finally:
        if paths is not None:
            worker_name = hashlib.sha256(run_id.encode()).hexdigest()[:16]
            try:
                status = tmux.status(name=worker_name, namespace=campaign.id, state_dir=paths.state_dir)
                if status.get("state") != "lost":
                    tmux.stop(name=worker_name, namespace=campaign.id, state_dir=paths.state_dir)
            except Exception:
                pass
            shutil.rmtree(paths.private, ignore_errors=True)
        store.close()
