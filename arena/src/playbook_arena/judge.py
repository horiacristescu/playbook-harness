"""Blind command-judge packets, strict cited claims, and visible disagreement."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from .case import ArenaCaseError
from .process import run_bounded
from .schema import CampaignDefinition, JudgeCommand, RubricDefinition, canonical_json
from .store import RunStore


def _expanded(argv: Sequence[str], *, packet: Path, artifacts: Path) -> list[str]:
    replacements = {"{packet}": str(packet), "{artifacts}": str(artifacts)}
    return [replacements.get(item, item) for item in argv]


def _copy_packet(store: RunStore, rubric: RubricDefinition, destination: Path, *, opaque_run_id: str) -> tuple[Path, set[str]]:
    artifacts_root = destination / "artifacts"
    artifacts_root.mkdir()
    records = store.verify_artifacts()
    allowed = set()
    catalog = []
    for record in records:
        path = record["path"]
        # Judge results never become input to another judge.
        if path.startswith("judges/"):
            continue
        source = store.artifacts.joinpath(*Path(path).parts)
        target = artifacts_root.joinpath(*Path(path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target, follow_symlinks=False)
        target.chmod(0o400)
        allowed.add(path)
        catalog.append({key: record[key] for key in ("kind", "path", "bytes", "sha256")})
    packet = {
        "schema": 1,
        "run_id": opaque_run_id,
        "rubric": {"id": rubric.id, "version": rubric.version, "criteria": [criterion.__dict__ for criterion in rubric.criteria]},
        "artifacts": catalog,
        "instructions": "Return strict JSON claims. Cite only artifact paths in this packet; do not repair or interact with the run.",
    }
    packet_path = destination / "packet.json"
    packet_path.write_bytes(canonical_json(packet) + b"\n")
    packet_path.chmod(0o400)
    return packet_path, allowed


def _validate_output(value: Any, *, judge_id: str, rubric: RubricDefinition, citations: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"schema", "judge_id", "claims"} or value["schema"] != 1 or value["judge_id"] != judge_id:
        raise ArenaCaseError("judge output envelope is invalid")
    if not isinstance(value["claims"], list) or not value["claims"]:
        raise ArenaCaseError("judge claims must be a non-empty list")
    expected = {criterion.id for criterion in rubric.criteria if criterion.kind == "semantic"}
    if not expected:
        expected = {criterion.id for criterion in rubric.criteria}
    claims = []
    seen = set()
    for index, raw in enumerate(value["claims"]):
        if not isinstance(raw, dict) or set(raw) != {"criterion_id", "disposition", "confidence", "rationale", "citations"}:
            raise ArenaCaseError(f"judge claim {index} fields are invalid")
        criterion = raw["criterion_id"]
        if criterion not in expected or criterion in seen:
            raise ArenaCaseError(f"judge claim criterion is unknown or duplicate: {criterion}")
        if raw["disposition"] not in {"pass", "fail", "uncertain"} or raw["confidence"] not in {"low", "medium", "high"}:
            raise ArenaCaseError(f"judge claim {criterion} disposition/confidence is invalid")
        if not isinstance(raw["rationale"], str) or not raw["rationale"].strip() or "\0" in raw["rationale"]:
            raise ArenaCaseError(f"judge claim {criterion} rationale is invalid")
        cited = raw["citations"]
        if not isinstance(cited, list) or not cited or not all(isinstance(item, str) and item in citations for item in cited):
            raise ArenaCaseError(f"judge claim {criterion} has missing or unknown citations")
        if len(cited) != len(set(cited)):
            raise ArenaCaseError(f"judge claim {criterion} citations contain duplicates")
        claims.append(raw)
        seen.add(criterion)
    if seen != expected:
        raise ArenaCaseError(f"judge output omitted criteria: {', '.join(sorted(expected - seen))}")
    return claims


def run_judges(store: RunStore, campaign: CampaignDefinition, rubric: RubricDefinition, *, opaque_run_id: str, runtime: Path) -> list[dict[str, Any]]:
    judgments = []
    for judge in campaign.judges:
        private = Path(tempfile.mkdtemp(prefix="pb-arena-judge-"))
        started = time.monotonic()
        status = "unscorable"
        claims: list[dict[str, Any]] = []
        error: str | None = None
        returncode: int | None = None
        stdout = b""
        stderr = b""
        try:
            packet, citations = _copy_packet(store, rubric, private, opaque_run_id=opaque_run_id)
            argv = _expanded(judge.argv, packet=packet, artifacts=private / "artifacts")
            store.append("judge_started", {"judge_id": judge.id, "argv": argv}, state="judging")
            coordinator_src = Path(__file__).resolve().parents[1]
            python_path = os.pathsep.join((str(coordinator_src), str(runtime / "arena/src"), str(runtime / "src")))
            environment = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": python_path, "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C", "LANG": "C", "PLAYBOOK_ARENA_PACKET": str(packet)}
            completed = run_bounded(argv, cwd=private, env=environment, timeout=campaign.limits["wall_seconds"], per_stream_limit=campaign.limits["max_output_bytes"])
            returncode, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
            if completed.environment_error is not None:
                error = f"judge environment failure: {completed.environment_error}"
            elif completed.timed_out:
                error = "judge timed out"
            elif completed.stdout_overflow or completed.stderr_overflow:
                error = "judge output exceeded frozen limit"
            elif returncode != 0:
                error = f"judge exited {returncode}"
            else:
                try:
                    value = json.loads(stdout.decode("utf-8"))
                    claims = _validate_output(value, judge_id=judge.id, rubric=rubric, citations=citations)
                    status = "scored"
                except (UnicodeError, json.JSONDecodeError, ArenaCaseError) as exc:
                    error = str(exc)
            raw_path = private / "raw.stdout"
            raw_path.write_bytes(stdout[: campaign.limits["max_output_bytes"]])
            err_path = private / "raw.stderr"
            err_path.write_bytes(stderr[: campaign.limits["max_output_bytes"]])
            raw_artifact = store.record_artifact(raw_path, f"judges/{judge.id}/raw.stdout", kind="judge-raw", state="judging")
            err_artifact = store.record_artifact(err_path, f"judges/{judge.id}/raw.stderr", kind="judge-raw", state="judging")
            result = {"judge_id": judge.id, "status": status, "returncode": returncode, "duration_ms": round((time.monotonic() - started) * 1000), "claims": claims, "error": error, "raw_stdout": raw_artifact["path"], "raw_stderr": err_artifact["path"]}
            result_path = private / "result.json"
            result_path.write_bytes(canonical_json(result) + b"\n")
            result_artifact = store.record_artifact(result_path, f"judges/{judge.id}/result.json", kind="judgment", state="judging")
            result["artifact"] = result_artifact["path"]
            store.append("judge_completed", result, state="judging")
            judgments.append(result)
        finally:
            shutil.rmtree(private, ignore_errors=True)
    return judgments


def aggregate_judgments(rubric: RubricDefinition, judgments: Sequence[dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for criterion in rubric.criteria:
        dispositions = [claim["disposition"] for judgment in judgments if judgment.get("status") == "scored" for claim in judgment.get("claims", []) if claim["criterion_id"] == criterion.id]
        if not dispositions:
            result[criterion.id] = {"status": "unscorable", "counts": {}}
            continue
        counts = Counter(dispositions)
        if len(counts) == 1:
            status = "unanimous"
        elif counts.most_common()[0][1] > len(dispositions) / 2:
            status = "majority"
        else:
            status = "disputed"
        result[criterion.id] = {"status": status, "counts": dict(sorted(counts.items()))}
    return result
