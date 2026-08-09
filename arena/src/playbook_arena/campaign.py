"""Freeze campaigns and derive opaque balanced assignments before execution."""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .case import ArenaCaseError
from .schema import (
    CampaignDefinition,
    RubricDefinition,
    ScriptDefinition,
    VariantDefinition,
    canonical_json,
    load_campaign,
    load_rubric,
    load_script,
    load_variant,
    resolve_reference,
)


FREEZE_SCHEMA = 1


@dataclass(frozen=True)
class Assignment:
    run_index: int
    repetition: int
    arm_id: str
    variant_id: str
    variant_sha256: str


@dataclass(frozen=True)
class FrozenCampaign:
    definition: CampaignDefinition
    script: ScriptDefinition
    rubric: RubricDefinition
    variants: tuple[VariantDefinition, ...]
    assignments: tuple[Assignment, ...]
    root: Path
    assignment_sha256: str


def _arm_id(campaign_hash: str, seed: int, variant_hash: str) -> str:
    digest = hashlib.sha256(f"{campaign_hash}\0{seed}\0{variant_hash}".encode()).hexdigest()
    return f"arm-{digest[:12]}"


def balanced_assignments(campaign: CampaignDefinition, variants: Iterable[VariantDefinition]) -> tuple[Assignment, ...]:
    values = tuple(variants)
    if not values:
        raise ArenaCaseError("campaign has no loaded variants")
    arms = {variant.id: _arm_id(campaign.sha256, campaign.seed, variant.sha256) for variant in values}
    if len(set(arms.values())) != len(arms):
        raise ArenaCaseError("opaque arm identifier collision")
    randomizer = random.Random(campaign.seed)
    result = []
    index = 0
    for repetition in range(1, campaign.repetitions + 1):
        block = list(values)
        randomizer.shuffle(block)
        for variant in block:
            index += 1
            result.append(Assignment(index, repetition, arms[variant.id], variant.id, variant.sha256))
    return tuple(result)


def _exclusive_json(path: Path, value: Any) -> None:
    encoded = canonical_json(value) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ArenaCaseError(f"cannot freeze campaign file {path}: {exc}") from exc
    try:
        offset = 0
        while offset < len(encoded):
            written = os.write(fd, encoded[offset:])
            if written <= 0:
                raise ArenaCaseError("short write while freezing campaign")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_exact(path: Path, expected: Any, *, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ArenaCaseError(f"frozen {label} is missing or not regular")
    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArenaCaseError(f"frozen {label} is unreadable: {exc}") from exc
    if actual != expected:
        raise ArenaCaseError(f"frozen {label} does not match requested campaign")


def _validate_tools(campaign: CampaignDefinition) -> None:
    required = set(campaign.required_tools)
    for command in [*(judge.argv for judge in campaign.judges), *(check.argv for check in campaign.checks)]:
        executable = command[0]
        if "{" not in executable:
            required.add(executable)
    missing = sorted(tool for tool in required if shutil.which(tool) is None)
    if missing:
        raise ArenaCaseError(f"campaign required tool(s) missing: {', '.join(missing)}")


def freeze_campaign(
    campaign_path: str | Path,
    results_root: str | Path,
    *,
    capabilities: Iterable[str] = (),
) -> FrozenCampaign:
    campaign = load_campaign(campaign_path)
    available = set(capabilities)
    missing_capabilities = sorted(set(campaign.required_capabilities) - available)
    if missing_capabilities:
        raise ArenaCaseError(f"campaign required capabilities missing: {', '.join(missing_capabilities)}")
    _validate_tools(campaign)
    script = load_script(resolve_reference(campaign.path, campaign.script, label="script"))
    rubric = load_rubric(resolve_reference(campaign.path, campaign.rubric, label="rubric"))
    variants = tuple(load_variant(resolve_reference(campaign.path, reference, label="variant")) for reference in campaign.variants)
    if len({variant.id for variant in variants}) != len(variants):
        raise ArenaCaseError("loaded variant IDs contain duplicates")
    if len({variant.base_commit for variant in variants}) != 1:
        raise ArenaCaseError("campaign variants must share one base commit")
    assignments = balanced_assignments(campaign, variants)
    assignment_value = {
        "schema": FREEZE_SCHEMA,
        "campaign_sha256": campaign.sha256,
        "assignments": [assignment.__dict__ for assignment in assignments],
    }
    assignment_hash = hashlib.sha256(canonical_json(assignment_value)).hexdigest()
    freeze_value = {
        "schema": FREEZE_SCHEMA,
        "campaign_id": campaign.id,
        "campaign_sha256": campaign.sha256,
        "script_sha256": script.sha256,
        "rubric_sha256": rubric.sha256,
        "variant_sha256": [variant.sha256 for variant in variants],
        "assignment_sha256": assignment_hash,
        "capabilities": sorted(available),
    }
    results = Path(results_root).expanduser()
    results.mkdir(parents=True, mode=0o700, exist_ok=True)
    if results.is_symlink() or not results.is_dir():
        raise ArenaCaseError(f"results root must be a real directory: {results}")
    root = results.resolve() / campaign.id
    if root.exists() or root.is_symlink():
        if root.is_symlink() or not root.is_dir():
            raise ArenaCaseError(f"campaign root must be a real directory: {root}")
        _read_exact(root / "campaign.freeze.json", freeze_value, label="campaign freeze")
        _read_exact(root / "assignment.json", assignment_value, label="assignment")
    else:
        staging = Path(tempfile.mkdtemp(prefix=f".{campaign.id}.freeze-", dir=results.resolve()))
        try:
            _exclusive_json(staging / "campaign.freeze.json", freeze_value)
            _exclusive_json(staging / "assignment.json", assignment_value)
            (staging / "runs").mkdir(mode=0o700)
            os.replace(staging, root)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return FrozenCampaign(campaign, script, rubric, variants, assignments, root, assignment_hash)


def execute_campaign(
    frozen: FrozenCampaign,
    *,
    cases_root: str | Path,
    sources: dict[str, Path],
    runtime_repo: str | Path,
    corpus: Path | None = None,
    transport=None,
) -> dict[str, Any]:
    from .analyze import analyze_campaign, write_report
    from .case import discover_cases
    from .run import resume_worker, run_identity, run_worker
    from .store import RunStore

    cases = {case.id: case for case in discover_cases(cases_root)}
    if frozen.definition.case_id not in cases:
        raise ArenaCaseError(f"campaign case is unavailable: {frozen.definition.case_id}")
    case = cases[frozen.definition.case_id]
    outcomes = []
    stopped = None
    for assignment in frozen.assignments:
        variant = next(item for item in frozen.variants if item.id == assignment.variant_id)
        run_id, _ = run_identity(frozen.definition, variant, arm_id=assignment.arm_id, repetition=assignment.repetition)
        run_root = frozen.root / "runs" / run_id
        if run_root.exists():
            try:
                with RunStore.read_only(run_root) as store:
                    finished = [event for event in store.verify() if event.type == "run_finished"]
                if len(finished) == 1:
                    outcomes.append({"run_id": run_id, "status": "existing"})
                    continue
                if finished:
                    raise ArenaCaseError("existing run has duplicate terminal events")
                outcome = resume_worker(campaign=frozen.definition, script=frozen.script, rubric=frozen.rubric, variant=variant, arm_id=assignment.arm_id, repetition=assignment.repetition, results_root=frozen.root / "runs", transport=transport)
                outcomes.append({"run_id": outcome.run_id, "status": "resumed"})
                continue
            except ArenaCaseError as exc:
                stopped = {"reason": "existing run could not be resumed", "run_id": run_id, "error": str(exc)}
                break
        try:
            outcome = run_worker(campaign=frozen.definition, case=case, script=frozen.script, rubric=frozen.rubric, variant=variant, arm_id=assignment.arm_id, repetition=assignment.repetition, results_root=frozen.root / "runs", sources=sources, runtime_repo=runtime_repo, corpus=corpus, transport=transport)
            outcomes.append({"run_id": outcome.run_id, "status": outcome.state})
        except Exception as exc:
            stopped = {"reason": "environment/integrity run failure", "run_id": run_id, "error": str(exc)}
            break
    if stopped is not None:
        stop_path = frozen.root / "campaign-stop.json"
        if not stop_path.exists():
            _exclusive_json(stop_path, {"schema": 1, **stopped})
    if stopped is not None:
        return {"campaign_id": frozen.definition.id, "outcomes": outcomes, "stopped": stopped, "recommendation": "RETEST", "report": None}
    report_json = frozen.root / "report.json"
    if report_json.exists():
        analysis = json.loads(report_json.read_text(encoding="utf-8"))
    else:
        _, _, analysis = write_report(frozen)
    return {"campaign_id": frozen.definition.id, "outcomes": outcomes, "stopped": None, "recommendation": analysis["recommendation"], "report": str(frozen.root / "report.md")}
