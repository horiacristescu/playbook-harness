"""Strict, frozen schemas for interactive historical arena inputs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .case import ArenaCaseError, IDENTIFIER_RE, SHA1_RE, SHA256_RE, safe_relative


SCHEMA = 1
ACK_RE_MAX = 512
TEXT_MAX = 64 * 1024
ARGV_MAX = 128


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _read(path: str | Path, *, label: str) -> tuple[Path, dict[str, Any]]:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ArenaCaseError(f"{label} must be a real file: {source}")
    try:
        raw = source.read_bytes()
        if len(raw) > 1024 * 1024:
            raise ArenaCaseError(f"{label} is too large: {source}")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArenaCaseError(f"cannot read {label}: {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise ArenaCaseError(f"{label} must be a JSON object")
    return source.resolve(), value


def _object(value: Any, fields: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ArenaCaseError(f"{label} must be an object")
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if unknown:
            detail.append(f"unknown {', '.join(unknown)}")
        raise ArenaCaseError(f"{label} fields invalid: {'; '.join(detail)}")
    return value


def _schema(value: Mapping[str, Any], *, label: str) -> None:
    if type(value["schema"]) is not int or value["schema"] != SCHEMA:
        raise ArenaCaseError(f"{label} schema must be {SCHEMA}")


def _text(value: Any, *, label: str, limit: int = TEXT_MAX) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value or len(value.encode("utf-8")) > limit:
        raise ArenaCaseError(f"{label} must be bounded non-empty text without NUL")
    return value


def _identifier(value: Any, *, label: str) -> str:
    text = _text(value, label=label, limit=64)
    if IDENTIFIER_RE.fullmatch(text) is None:
        raise ArenaCaseError(f"{label} is not a safe identifier: {text!r}")
    return text


def _integer(value: Any, *, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise ArenaCaseError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _strings(value: Any, *, label: str, identifiers: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ArenaCaseError(f"{label} must be a list")
    parser = _identifier if identifiers else _text
    result = tuple(parser(item, label=f"{label} item") for item in value)
    if len(result) != len(set(result)):
        raise ArenaCaseError(f"{label} contains duplicates")
    return result


def _argv(value: Any, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > ARGV_MAX:
        raise ArenaCaseError(f"{label} must be a non-empty bounded argv list")
    return tuple(_text(item, label=f"{label} item", limit=4096) for item in value)


@dataclass(frozen=True)
class ScriptEvent:
    id: str
    message: str
    deliver_when: str
    after: str | None
    acknowledge: str
    timeout_seconds: int
    on_timeout: str
    known_facts: tuple[str, ...]
    allowed_adaptations: Mapping[str, str]
    forbidden_disclosures: tuple[str, ...]


@dataclass(frozen=True)
class ScriptDefinition:
    path: Path
    id: str
    events: tuple[ScriptEvent, ...]
    sha256: str


def load_script(path: str | Path) -> ScriptDefinition:
    source, raw = _read(path, label="script")
    value = _object(raw, {"schema", "id", "events"}, label="script")
    _schema(value, label="script")
    script_id = _identifier(value["id"], label="script id")
    if not isinstance(value["events"], list) or not value["events"]:
        raise ArenaCaseError("script events must be a non-empty list")
    events: list[ScriptEvent] = []
    seen: set[str] = set()
    for index, item in enumerate(value["events"]):
        event = _object(
            item,
            {"id", "message", "deliver_when", "after", "acknowledge", "timeout_seconds", "on_timeout", "known_facts", "allowed_adaptations", "forbidden_disclosures"},
            label=f"script event {index}",
        )
        event_id = _identifier(event["id"], label="event id")
        if event_id in seen:
            raise ArenaCaseError(f"duplicate script event id: {event_id}")
        trigger = event["deliver_when"]
        if trigger not in {"start", "after_ack", "after_worker_exit"}:
            raise ArenaCaseError("deliver_when must be start, after_ack, or after_worker_exit")
        after = event["after"]
        if after is not None:
            after = _identifier(after, label="event predecessor")
        if index == 0 and (trigger != "start" or after is not None):
            raise ArenaCaseError("first script event must use start with no predecessor")
        if index > 0 and (after != events[-1].id or trigger == "start"):
            raise ArenaCaseError("later script events must follow the immediately preceding event")
        pattern = _text(event["acknowledge"], label="acknowledgment regex", limit=ACK_RE_MAX)
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ArenaCaseError(f"invalid acknowledgment regex: {exc}") from exc
        adaptations = event["allowed_adaptations"]
        if not isinstance(adaptations, dict):
            raise ArenaCaseError("allowed_adaptations must be an object")
        parsed_adaptations = {
            _identifier(key, label="adaptation id"): _text(text, label="adaptation text")
            for key, text in adaptations.items()
        }
        timeout_action = event["on_timeout"]
        if timeout_action not in {"fail", "skip", "stop"}:
            raise ArenaCaseError("on_timeout must be fail, skip, or stop")
        events.append(
            ScriptEvent(
                id=event_id,
                message=_text(event["message"], label="event message"),
                deliver_when=trigger,
                after=after,
                acknowledge=pattern,
                timeout_seconds=_integer(event["timeout_seconds"], label="event timeout", minimum=1, maximum=86_400),
                on_timeout=timeout_action,
                known_facts=_strings(event["known_facts"], label="known_facts"),
                allowed_adaptations=parsed_adaptations,
                forbidden_disclosures=_strings(event["forbidden_disclosures"], label="forbidden_disclosures"),
            )
        )
        seen.add(event_id)
    return ScriptDefinition(source, script_id, tuple(events), content_hash(raw))


@dataclass(frozen=True)
class RubricCriterion:
    id: str
    kind: str
    text: str


@dataclass(frozen=True)
class RubricDefinition:
    path: Path
    id: str
    version: str
    criteria: tuple[RubricCriterion, ...]
    sha256: str


def load_rubric(path: str | Path) -> RubricDefinition:
    source, raw = _read(path, label="rubric")
    value = _object(raw, {"schema", "id", "version", "criteria"}, label="rubric")
    _schema(value, label="rubric")
    if not isinstance(value["criteria"], list) or not value["criteria"]:
        raise ArenaCaseError("rubric criteria must be a non-empty list")
    criteria = []
    seen = set()
    for index, item in enumerate(value["criteria"]):
        entry = _object(item, {"id", "kind", "text"}, label=f"rubric criterion {index}")
        criterion_id = _identifier(entry["id"], label="criterion id")
        if criterion_id in seen:
            raise ArenaCaseError(f"duplicate rubric criterion: {criterion_id}")
        if entry["kind"] not in {"deterministic", "semantic"}:
            raise ArenaCaseError("criterion kind must be deterministic or semantic")
        criteria.append(RubricCriterion(criterion_id, entry["kind"], _text(entry["text"], label="criterion text")))
        seen.add(criterion_id)
    return RubricDefinition(source, _identifier(value["id"], label="rubric id"), _identifier(value["version"], label="rubric version"), tuple(criteria), content_hash(raw))


@dataclass(frozen=True)
class VariantDefinition:
    path: Path
    id: str
    base_commit: str
    patch: str | None
    patch_sha256: str | None
    touched_paths: tuple[str, ...]
    rationale: str
    sha256: str


def load_variant(path: str | Path) -> VariantDefinition:
    source, raw = _read(path, label="variant")
    value = _object(raw, {"schema", "id", "base_commit", "patch", "patch_sha256", "touched_paths", "rationale"}, label="variant")
    _schema(value, label="variant")
    commit = _text(value["base_commit"], label="variant base commit", limit=40)
    if SHA1_RE.fullmatch(commit) is None:
        raise ArenaCaseError("variant base_commit must be a full lowercase SHA-1")
    patch = value["patch"]
    digest = value["patch_sha256"]
    touched = tuple(safe_relative(item, label="variant touched path") for item in _strings(value["touched_paths"], label="touched_paths"))
    if patch is None:
        if digest is not None or touched:
            raise ArenaCaseError("control variant needs null patch/hash and no touched paths")
    else:
        patch = safe_relative(patch, label="variant patch")
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise ArenaCaseError("variant patch_sha256 must be 64 lowercase hex characters")
        patch_path = source.parent.joinpath(*Path(patch).parts)
        if patch_path.is_symlink() or not patch_path.is_file():
            raise ArenaCaseError(f"variant patch must be a real file: {patch}")
        if hashlib.sha256(patch_path.read_bytes()).hexdigest() != digest:
            raise ArenaCaseError("variant patch hash mismatch")
        if not touched:
            raise ArenaCaseError("patched variant must declare touched paths")
    return VariantDefinition(source, _identifier(value["id"], label="variant id"), commit, patch, digest, touched, _text(value["rationale"], label="variant rationale"), content_hash(raw))


@dataclass(frozen=True)
class FileReference:
    path: str
    sha256: str


@dataclass(frozen=True)
class CommandCheck:
    id: str
    argv: tuple[str, ...]
    timeout_seconds: int


@dataclass(frozen=True)
class JudgeCommand:
    id: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class CampaignDefinition:
    path: Path
    id: str
    case_id: str
    script: FileReference
    rubric: FileReference
    variants: tuple[FileReference, ...]
    repetitions: int
    seed: int
    worker_agent: str
    worker_args: tuple[str, ...]
    worker_model: str | None
    required_tools: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    judges: tuple[JudgeCommand, ...]
    checks: tuple[CommandCheck, ...]
    limits: Mapping[str, int]
    decision: Mapping[str, int]
    sha256: str


def _reference(value: Any, *, label: str) -> FileReference:
    item = _object(value, {"path", "sha256"}, label=label)
    path = safe_relative(item["path"], label=f"{label} path")
    digest = item["sha256"]
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise ArenaCaseError(f"{label} sha256 must be 64 lowercase hex characters")
    return FileReference(path, digest)


def load_campaign(path: str | Path) -> CampaignDefinition:
    source, raw = _read(path, label="campaign")
    value = _object(raw, {"schema", "id", "case_id", "script", "rubric", "variants", "repetitions", "seed", "worker", "judges", "checks", "limits", "decision"}, label="campaign")
    _schema(value, label="campaign")
    worker = _object(value["worker"], {"agent", "args", "model", "required_tools", "required_capabilities"}, label="worker")
    model = worker["model"]
    if model is not None:
        model = _text(model, label="worker model", limit=256)
    if not isinstance(value["variants"], list) or not value["variants"]:
        raise ArenaCaseError("campaign variants must be a non-empty list")
    variants = tuple(_reference(item, label="variant reference") for item in value["variants"])
    if len({item.path for item in variants}) != len(variants):
        raise ArenaCaseError("campaign variant paths contain duplicates")
    if not isinstance(value["judges"], list) or not value["judges"]:
        raise ArenaCaseError("campaign judges must be a non-empty list")
    judges = []
    for index, item in enumerate(value["judges"]):
        judge = _object(item, {"id", "argv"}, label=f"judge {index}")
        judges.append(JudgeCommand(_identifier(judge["id"], label="judge id"), _argv(judge["argv"], label="judge argv")))
    if len({judge.id for judge in judges}) != len(judges):
        raise ArenaCaseError("judge IDs contain duplicates")
    if not isinstance(value["checks"], list):
        raise ArenaCaseError("checks must be a list")
    checks = []
    for index, item in enumerate(value["checks"]):
        check = _object(item, {"id", "argv", "timeout_seconds"}, label=f"check {index}")
        checks.append(CommandCheck(_identifier(check["id"], label="check id"), _argv(check["argv"], label="check argv"), _integer(check["timeout_seconds"], label="check timeout", minimum=1, maximum=86_400)))
    if len({check.id for check in checks}) != len(checks):
        raise ArenaCaseError("check IDs contain duplicates")
    limits = _object(value["limits"], {"wall_seconds", "max_interventions", "max_output_bytes", "max_restarts"}, label="limits")
    parsed_limits = {
        "wall_seconds": _integer(limits["wall_seconds"], label="wall_seconds", minimum=1, maximum=604_800),
        "max_interventions": _integer(limits["max_interventions"], label="max_interventions", minimum=1, maximum=10_000),
        "max_output_bytes": _integer(limits["max_output_bytes"], label="max_output_bytes", minimum=1, maximum=1_073_741_824),
        "max_restarts": _integer(limits["max_restarts"], label="max_restarts", minimum=0, maximum=100),
    }
    decision = _object(value["decision"], {"adopt_min_passes", "reject_min_failures"}, label="decision")
    parsed_decision = {
        "adopt_min_passes": _integer(decision["adopt_min_passes"], label="adopt_min_passes", minimum=1, maximum=1_000_000),
        "reject_min_failures": _integer(decision["reject_min_failures"], label="reject_min_failures", minimum=1, maximum=1_000_000),
    }
    seed = value["seed"]
    if type(seed) is not int or seed < 0 or seed > 2**63 - 1:
        raise ArenaCaseError("campaign seed must be a non-negative 63-bit integer")
    return CampaignDefinition(
        path=source,
        id=_identifier(value["id"], label="campaign id"),
        case_id=_identifier(value["case_id"], label="case id"),
        script=_reference(value["script"], label="script reference"),
        rubric=_reference(value["rubric"], label="rubric reference"),
        variants=variants,
        repetitions=_integer(value["repetitions"], label="repetitions", minimum=1, maximum=10_000),
        seed=seed,
        worker_agent=_identifier(worker["agent"], label="worker agent"),
        worker_args=_argv(worker["args"], label="worker args"),
        worker_model=model,
        required_tools=_strings(worker["required_tools"], label="required_tools", identifiers=True),
        required_capabilities=_strings(worker["required_capabilities"], label="required_capabilities", identifiers=True),
        judges=tuple(judges),
        checks=tuple(checks),
        limits=parsed_limits,
        decision=parsed_decision,
        sha256=content_hash(raw),
    )


def resolve_reference(owner: Path, reference: FileReference, *, label: str) -> Path:
    root = owner.parent.resolve()
    target = root.joinpath(*Path(reference.path).parts)
    if target.is_symlink() or not target.is_file():
        raise ArenaCaseError(f"{label} must be a real file: {reference.path}")
    resolved = target.resolve()
    if root not in resolved.parents:
        raise ArenaCaseError(f"{label} escapes campaign directory: {reference.path}")
    if hashlib.sha256(resolved.read_bytes()).hexdigest() != reference.sha256:
        raise ArenaCaseError(f"{label} hash mismatch: {reference.path}")
    return resolved
