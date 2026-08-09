"""Multidimensional campaign analysis and compact bounded recommendations."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .campaign import FrozenCampaign
from .case import ArenaCaseError
from .run import run_identity
from .schema import canonical_json
from .store import RunStore


def _run_summary(frozen: FrozenCampaign, assignment) -> dict[str, Any]:
    variant = next(item for item in frozen.variants if item.id == assignment.variant_id)
    run_id, _ = run_identity(frozen.definition, variant, arm_id=assignment.arm_id, repetition=assignment.repetition)
    root = frozen.root / "runs" / run_id
    if not root.exists():
        return {"run_id": run_id, "arm_id": assignment.arm_id, "repetition": assignment.repetition, "status": "missing", "evidence": f"runs/{run_id}", "checks_passed": 0, "checks_failed": 0, "interventions": 0, "deviations": 0, "duration_ms": None, "judges_scored": 0, "judges_unscorable": len(frozen.definition.judges), "disputed": 0, "integrity": False}
    try:
        with RunStore.read_only(root) as store:
            events = store.verify()
            finished = [event.payload for event in events if event.type == "run_finished"]
            if len(finished) != 1:
                raise ArenaCaseError("run needs exactly one terminal run_finished event")
            payload = finished[0]
            final_state = store.read_state().get("state")
            if final_state not in {"completed", "failed"}:
                raise ArenaCaseError(f"run terminal state is invalid: {final_state}")
            judgment = json.loads((store.artifacts / "judges/summary.json").read_text(encoding="utf-8"))
            disputed = sum(value["status"] == "disputed" for value in judgment.get("criteria", {}).values())
            semantic_failures = sum(value.get("counts", {}).get("fail", 0) > 0 for value in judgment.get("criteria", {}).values())
            return {"run_id": run_id, "arm_id": assignment.arm_id, "repetition": assignment.repetition, "status": final_state, "evidence": f"runs/{run_id}", "checks_passed": payload.get("checks_passed", 0), "checks_failed": payload.get("checks_failed", 0), "interventions": payload.get("interventions", 0), "deviations": payload.get("deviations", 0), "duration_ms": payload.get("duration_ms"), "judges_scored": payload.get("judges_scored", 0), "judges_unscorable": payload.get("judges_unscorable", 0), "disputed": disputed, "semantic_failures": semantic_failures, "integrity": True}
    except (ArenaCaseError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"run_id": run_id, "arm_id": assignment.arm_id, "repetition": assignment.repetition, "status": "invalid", "error": str(exc), "evidence": f"runs/{run_id}", "checks_passed": 0, "checks_failed": 0, "interventions": 0, "deviations": 0, "duration_ms": None, "judges_scored": 0, "judges_unscorable": len(frozen.definition.judges), "disputed": 0, "semantic_failures": 0, "integrity": False}


def analyze_campaign(frozen: FrozenCampaign) -> dict[str, Any]:
    runs = [_run_summary(frozen, assignment) for assignment in frozen.assignments]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        grouped[run["arm_id"]].append(run)
    arms = []
    for arm_id in sorted(grouped):
        values = grouped[arm_id]
        durations = [item["duration_ms"] for item in values if isinstance(item["duration_ms"], int)]
        ceiling_violations = sum((isinstance(item["duration_ms"], int) and item["duration_ms"] > frozen.definition.limits["wall_seconds"] * 1000) or item["interventions"] > frozen.definition.limits["max_interventions"] for item in values)
        arms.append({
            "arm_id": arm_id,
            "runs": len(values),
            "completed": sum(item["status"] == "completed" for item in values),
            "outcome": {"checks_passed": sum(item["checks_passed"] for item in values), "checks_failed": sum(item["checks_failed"] for item in values)},
            "attention": {"interventions": sum(item["interventions"] for item in values), "deviations": sum(item["deviations"] for item in values)},
            "cost": {"duration_ms": sum(durations), "model_tokens": None, "currency": None},
            "safety": {"integrity_failures": sum(not item["integrity"] for item in values), "ceiling_violations": ceiling_violations},
            "epistemic": {"judges_scored": sum(item["judges_scored"] for item in values), "judges_unscorable": sum(item["judges_unscorable"] for item in values), "disputed_criteria": sum(item["disputed"] for item in values), "semantic_failures": sum(item.get("semantic_failures", 0) for item in values)},
            "evidence": [item["evidence"] for item in values],
        })
    controls = {assignment.arm_id for assignment in frozen.assignments for variant in frozen.variants if assignment.variant_id == variant.id and variant.patch is None}
    treatments = [arm for arm in arms if arm["arm_id"] not in controls]
    control = next((arm for arm in arms if arm["arm_id"] in controls), None) if len(controls) == 1 else None
    integrity_ok = all(arm["safety"]["integrity_failures"] == 0 and arm["safety"]["ceiling_violations"] == 0 for arm in arms)
    recommendation = "RETEST"
    reason = "insufficient or non-comparable evidence"
    if control is not None and treatments and integrity_ok:
        adoptable = [arm for arm in treatments if arm["completed"] == arm["runs"] and control["completed"] == control["runs"] and arm["outcome"]["checks_passed"] >= frozen.definition.decision["adopt_min_passes"] and arm["outcome"]["checks_passed"] > control["outcome"]["checks_passed"] and arm["outcome"]["checks_failed"] <= control["outcome"]["checks_failed"] and arm["epistemic"]["judges_unscorable"] == 0 and arm["epistemic"]["disputed_criteria"] == 0 and arm["epistemic"]["semantic_failures"] == 0]
        if len(adoptable) == 1:
            recommendation = "ADOPT"
            reason = f"{adoptable[0]['arm_id']} met the frozen adoption rule and dominated control on deterministic outcomes"
        elif treatments and all(arm["outcome"]["checks_failed"] >= frozen.definition.decision["reject_min_failures"] and arm["outcome"]["checks_passed"] <= control["outcome"]["checks_passed"] for arm in treatments):
            recommendation = "REJECT"
            reason = "all treatment arms met the frozen failure rule without improving deterministic outcomes"
    if not integrity_ok:
        recommendation, reason = "RETEST", "integrity or missing-run failure invalidates comparison"
    observations = {
        "representative": [next(item["evidence"] for item in runs if item["arm_id"] == arm["arm_id"]) for arm in arms],
        "disagreements": [item["evidence"] for item in runs if item["disputed"]],
        "adverse_or_invalid": [item["evidence"] for item in runs if not item["integrity"] or item["checks_failed"]],
    }
    return {"schema": 1, "campaign_id": frozen.definition.id, "campaign_sha256": frozen.definition.sha256, "assignment_sha256": frozen.assignment_sha256, "arms": arms, "runs": runs, "ceilings": {"wall_seconds_per_run": frozen.definition.limits["wall_seconds"], "max_interventions_per_run": frozen.definition.limits["max_interventions"], "model_cost": "not observed", "violations": sum(arm["safety"]["ceiling_violations"] for arm in arms)}, "observations": observations, "recommendation": recommendation, "reason": reason, "limitations": ["model tokens and currency are unobserved unless a future provider emits trusted usage", "command judges are evidence sources, not ground truth", "same-account role boundaries are auditable but not an OS security boundary"]}


def write_report(frozen: FrozenCampaign) -> tuple[Path, Path, dict[str, Any]]:
    analysis = analyze_campaign(frozen)
    if any(run["status"] in {"missing", "invalid"} for run in analysis["runs"]):
        raise ArenaCaseError("campaign is incomplete or invalid; refusing final report")
    json_path = frozen.root / "report.json"
    markdown_path = frozen.root / "report.md"
    if json_path.exists() or markdown_path.exists() or json_path.is_symlink() or markdown_path.is_symlink():
        raise ArenaCaseError("campaign report already exists; reports are immutable")
    json_path.write_bytes(canonical_json(analysis) + b"\n")
    lines = [f"# Arena report: {analysis['campaign_id']}", "", f"Decision: **{analysis['recommendation']}** — {analysis['reason']}", "", f"Campaign `{analysis['campaign_sha256']}`; assignment `{analysis['assignment_sha256']}`.", "", "| Opaque arm | Runs | Checks pass/fail | Interventions/deviations | Duration ms | Integrity failures | Judges scored/unscorable/disputed/fail |", "|---|---:|---:|---:|---:|---:|---:|"]
    for arm in analysis["arms"]:
        lines.append(f"| `{arm['arm_id']}` | {arm['completed']}/{arm['runs']} | {arm['outcome']['checks_passed']}/{arm['outcome']['checks_failed']} | {arm['attention']['interventions']}/{arm['attention']['deviations']} | {arm['cost']['duration_ms']} | {arm['safety']['integrity_failures']} | {arm['epistemic']['judges_scored']}/{arm['epistemic']['judges_unscorable']}/{arm['epistemic']['disputed_criteria']}/{arm['epistemic']['semantic_failures']} |")
    lines.extend(["", "Evidence:", *[f"- `{run['run_id']}`: `{run['status']}` → `{run['evidence']}`" for run in analysis["runs"]], "", f"Disagreements: {', '.join(analysis['observations']['disagreements']) or 'none'}", f"Adverse/invalid: {', '.join(analysis['observations']['adverse_or_invalid']) or 'none'}", "", "Limitations:", *[f"- {item}" for item in analysis["limitations"]], ""])
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path, analysis
