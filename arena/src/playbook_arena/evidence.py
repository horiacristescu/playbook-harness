"""Frozen post-run deterministic checks and explicit evidence missingness."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any, Sequence

from .case import ArenaCaseError
from .process import run_bounded
from .schema import CampaignDefinition, canonical_json
from .store import RunStore


def _expanded(argv: Sequence[str], *, workspace: Path, runtime: Path, packet: Path) -> list[str]:
    replacements = {"{workspace}": str(workspace), "{runtime}": str(runtime), "{packet}": str(packet)}
    return [replacements.get(item, item) for item in argv]


def _bounded(data: bytes, limit: int) -> tuple[bytes, dict[str, Any]]:
    digest = hashlib.sha256(data).hexdigest()
    if len(data) <= limit:
        return data, {"bytes": len(data), "sha256": digest, "truncated": False}
    return data[:limit], {"bytes": len(data), "sha256": digest, "truncated": True, "retained_bytes": limit}


def run_deterministic_checks(
    store: RunStore,
    campaign: CampaignDefinition,
    *,
    workspace: Path,
    runtime: Path,
    packet: Path,
    evidence_dir: Path,
) -> list[dict[str, Any]]:
    results = []
    per_stream_limit = max(1, campaign.limits["max_output_bytes"] // 2)
    python_path = os.pathsep.join((str(runtime / "arena/src"), str(runtime / "src")))
    environment = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": python_path, "PLAYBOOK_RUNTIME_DIR": str(runtime), "PLAYBOOK_ARENA_PACKET": str(packet), "LC_ALL": "C", "LANG": "C"}
    for check in campaign.checks:
        argv = _expanded(check.argv, workspace=workspace, runtime=runtime, packet=packet)
        store.append("check_started", {"check_id": check.id, "argv": argv, "timeout_seconds": check.timeout_seconds}, state="checking")
        started = time.monotonic()
        status = "completed"
        exit_status: int | None
        stdout = b""
        stderr = b""
        completed = run_bounded(argv, cwd=workspace, env=environment, timeout=check.timeout_seconds, per_stream_limit=per_stream_limit)
        stdout, stderr = completed.stdout, completed.stderr
        exit_status = completed.returncode
        if completed.environment_error is not None:
            status = "environment_failure"
            exit_status = None
            stderr = completed.environment_error.encode("utf-8", errors="replace")
            passed = False
        elif completed.timed_out:
            status = "timeout"
            exit_status = None
            passed = False
        elif completed.stdout_overflow or completed.stderr_overflow:
            status = "output_limit"
            passed = False
        else:
            passed = exit_status == 0
        retained_stdout, stdout_meta = _bounded(stdout, per_stream_limit)
        retained_stderr, stderr_meta = _bounded(stderr, per_stream_limit)
        if completed.stdout_overflow:
            stdout_meta.update({"truncated": True, "bytes": None, "sha256": None, "retained_bytes": len(retained_stdout)})
        if completed.stderr_overflow:
            stderr_meta.update({"truncated": True, "bytes": None, "sha256": None, "retained_bytes": len(retained_stderr)})
        stdout_path = evidence_dir / f"{check.id}.stdout"
        stderr_path = evidence_dir / f"{check.id}.stderr"
        stdout_path.write_bytes(retained_stdout)
        stderr_path.write_bytes(retained_stderr)
        stdout_artifact = store.record_artifact(stdout_path, f"checks/{check.id}.stdout", kind="check-output", state="checking")
        stderr_artifact = store.record_artifact(stderr_path, f"checks/{check.id}.stderr", kind="check-output", state="checking")
        result = {
            "check_id": check.id,
            "argv": argv,
            "status": status,
            "passed": passed,
            "exit_status": exit_status,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "stdout": {**stdout_meta, "artifact": stdout_artifact["path"]},
            "stderr": {**stderr_meta, "artifact": stderr_artifact["path"]},
        }
        result_path = evidence_dir / f"{check.id}.json"
        result_path.write_bytes(canonical_json(result) + b"\n")
        result_artifact = store.record_artifact(result_path, f"checks/{check.id}.json", kind="check-result", state="checking")
        result["artifact"] = result_artifact["path"]
        store.append("check_completed", result, state="checking")
        results.append(result)
    summary = {"schema": 1, "checks": results, "missing": [] if campaign.checks else ["campaign declares no deterministic checks"]}
    summary_path = evidence_dir / "summary.json"
    summary_path.write_bytes(canonical_json(summary) + b"\n")
    store.record_artifact(summary_path, "checks/summary.json", kind="check-summary", state="collecting")
    return results
