"""Public CLI for historical case inspection and reconstruction."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from .analyze import write_report
from .case import ArenaCaseError, CaseDefinition, discover_cases
from .campaign import execute_campaign, freeze_campaign
from .git_source import parse_source_bindings
from .prepare import PROVENANCE_FILE, prepare_case, tree_digest


def default_cases_root() -> Path:
    return Path(__file__).resolve().parents[2] / "cases"


def default_canaries_root() -> Path:
    return Path(__file__).resolve().parents[2] / "canaries"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pb-arena", description="Reconstruct and diagnose portable historical cases."
    )
    parser.add_argument("--cases-dir", default=str(default_cases_root()), help="explicit case collection root")
    domains = parser.add_subparsers(dest="domain", required=True)
    case = domains.add_parser("case", help="historical case operations")
    commands = case.add_subparsers(dest="case_command", required=True)

    listing = commands.add_parser("list", help="validate and list available cases")
    listing.add_argument("--json", action="store_true")
    for name in ("prepare", "doctor"):
        command = commands.add_parser(name, help=f"{name} one historical case")
        command.add_argument("case_id")
        if name == "prepare":
            command.add_argument("destination")
        command.add_argument("--source", action="append", default=[], metavar="ID=PATH")
        command.add_argument("--corpus")
        command.add_argument("--json", action="store_true")
    campaign = domains.add_parser("campaign", help="frozen campaign operations")
    campaign_commands = campaign.add_subparsers(dest="campaign_command", required=True)
    for name in ("freeze", "run", "report"):
        command = campaign_commands.add_parser(name, help=f"{name} a frozen campaign")
        command.add_argument("campaign")
        command.add_argument("--results", required=True, help="explicit append-only results root")
        command.add_argument("--capability", action="append", default=[])
        if name == "run":
            command.add_argument("--source", action="append", default=[], metavar="ID=PATH")
            command.add_argument("--runtime-repo", required=True)
            command.add_argument("--corpus")
        command.add_argument("--json", action="store_true")
    canary = domains.add_parser("canary", help="shipped network-free mechanics canary")
    canary_commands = canary.add_subparsers(dest="canary_command", required=True)
    canary_list = canary_commands.add_parser("list", help="list shipped canaries")
    canary_list.add_argument("--json", action="store_true")
    canary_run = canary_commands.add_parser("run", help="run one shipped canary")
    canary_run.add_argument("canary_id")
    canary_run.add_argument("--results", required=True)
    canary_run.add_argument("--source", action="append", default=[], metavar="ID=PATH")
    canary_run.add_argument("--runtime-repo", required=True)
    canary_run.add_argument("--corpus")
    canary_run.add_argument("--json", action="store_true")
    return parser


def _cases(root: str) -> dict[str, CaseDefinition]:
    values = discover_cases(root)
    return {case.id: case for case in values}


def _select(root: str, case_id: str) -> CaseDefinition:
    cases = _cases(root)
    if case_id not in cases:
        raise ArenaCaseError(f"unknown case {case_id!r}; use `pb-arena case list`")
    return cases[case_id]


def _summary(case: CaseDefinition) -> dict[str, Any]:
    return {
        "id": case.id,
        "title": case.title,
        "fidelity": case.fidelity,
        "caveats": list(case.caveats),
        "source_id": case.source.id,
        "source_commit": case.source.commit,
        "prepared_tree_sha256": case.prepared_tree_sha256,
    }


def _manifest(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "executable": bool(path.stat().st_mode & 0o111),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and path.relative_to(root).as_posix() != PROVENANCE_FILE
    ]


def doctor_case(
    case: CaseDefinition,
    *,
    sources: dict[str, Path],
    corpus: Path | None,
) -> dict[str, Any]:
    missing_tools = [tool for tool in case.tools if shutil.which(tool) is None]
    if missing_tools:
        raise ArenaCaseError(f"required tool(s) missing: {', '.join(missing_tools)}")
    with tempfile.TemporaryDirectory(prefix=f"pb-arena-doctor-{case.id}-") as temporary:
        root = Path(temporary)
        first = root / "first" / "workspace"
        second = root / "different-parent" / "workspace"
        first.parent.mkdir()
        second.parent.mkdir()
        one = prepare_case(case, first, sources=sources, corpus=corpus)
        two = prepare_case(case, second, sources=sources, corpus=corpus)
        if one != two or tree_digest(first) != tree_digest(second) or _manifest(first) != _manifest(second):
            raise ArenaCaseError("doctor path-independence comparison failed")
        if any(path.name == ".git" for path in first.rglob(".git")):
            raise ArenaCaseError("doctor found Git metadata in prepared workspace")
        return {
            "case_id": case.id,
            "status": "pass",
            "fidelity": case.fidelity,
            "caveats": list(case.caveats),
            "prepared_tree_sha256": case.prepared_tree_sha256,
            "files": len(_manifest(first)),
            "path_independent": True,
            "git_metadata_absent": True,
        }


def _emit(value: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
    elif isinstance(value, list):
        for item in value:
            suffix = f" ({item['fidelity']})"
            if item["caveats"]:
                suffix += f" — {item['caveats'][0]}"
            print(f"{item['id']}: {item['title']}{suffix}")
    else:
        print(f"{value['case_id']}: {value.get('status', 'prepared')} {value['prepared_tree_sha256']}")


def main(arguments: list[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    as_json = bool(options.json)
    if options.domain == "case":
        if options.case_command == "list":
            _emit([_summary(case) for case in _cases(options.cases_dir).values()], as_json=as_json)
            return 0
        case = _select(options.cases_dir, options.case_id)
        sources = parse_source_bindings(options.source)
        unknown_sources = sorted(set(sources) - {case.source.id})
        if unknown_sources:
            raise ArenaCaseError(f"source binding does not belong to case: {', '.join(unknown_sources)}")
        corpus = None if options.corpus is None else Path(options.corpus).expanduser()
        if options.case_command == "prepare":
            value = prepare_case(case, options.destination, sources=sources, corpus=corpus)
            result = {"case_id": case.id, "status": "prepared", **value}
        else:
            result = doctor_case(case, sources=sources, corpus=corpus)
        _emit(result, as_json=as_json)
        return 0
    if options.domain == "canary" and options.canary_command == "list":
        values = sorted(path.parent.name for path in default_canaries_root().glob("*/campaign.json") if path.is_file() and not path.is_symlink())
        print(json.dumps(values, indent=2) if as_json else "\n".join(values))
        return 0
    if options.domain == "canary":
        campaign_path = default_canaries_root() / options.canary_id / "campaign.json"
        if campaign_path.is_symlink() or not campaign_path.is_file():
            raise ArenaCaseError(f"unknown canary: {options.canary_id}")
        frozen = freeze_campaign(campaign_path, options.results)
        result = execute_campaign(frozen, cases_root=options.cases_dir, sources=parse_source_bindings(options.source), runtime_repo=options.runtime_repo, corpus=None if options.corpus is None else Path(options.corpus).expanduser())
    else:
        frozen = freeze_campaign(options.campaign, options.results, capabilities=options.capability)
        if options.campaign_command == "freeze":
            result = {"campaign_id": frozen.definition.id, "campaign_sha256": frozen.definition.sha256, "assignment_sha256": frozen.assignment_sha256, "runs": len(frozen.assignments), "root": str(frozen.root)}
        elif options.campaign_command == "run":
            result = execute_campaign(frozen, cases_root=options.cases_dir, sources=parse_source_bindings(options.source), runtime_repo=options.runtime_repo, corpus=None if options.corpus is None else Path(options.corpus).expanduser())
        else:
            report_path = frozen.root / "report.json"
            if report_path.exists():
                result = json.loads(report_path.read_text(encoding="utf-8"))
            else:
                _, _, result = write_report(frozen)
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif "recommendation" in result:
        print(f"{result.get('campaign_id', frozen.definition.id)}: {result['recommendation']} ({result.get('report', frozen.root / 'report.md')})")
    else:
        print(f"{result['campaign_id']}: frozen {result['assignment_sha256']}")
    return 0


def entrypoint() -> int:
    try:
        return main()
    except ArenaCaseError as exc:
        if "--json" in sys.argv[1:]:
            print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(entrypoint())
