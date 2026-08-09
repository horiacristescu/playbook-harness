"""Public CLI for historical case inspection and reconstruction."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from .case import ArenaCaseError, CaseDefinition, discover_cases
from .git_source import parse_source_bindings
from .prepare import PROVENANCE_FILE, prepare_case, tree_digest


def default_cases_root() -> Path:
    return Path(__file__).resolve().parents[2] / "cases"


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
    if options.case_command == "list":
        _emit([_summary(case) for case in _cases(options.cases_dir).values()], as_json=as_json)
        return 0
    case = _select(options.cases_dir, options.case_id)
    sources = parse_source_bindings(options.source)
    corpus = None if options.corpus is None else Path(options.corpus).expanduser()
    if options.case_command == "prepare":
        value = prepare_case(case, options.destination, sources=sources, corpus=corpus)
        result = {"case_id": case.id, "status": "prepared", **value}
    else:
        result = doctor_case(case, sources=sources, corpus=corpus)
    _emit(result, as_json=as_json)
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
