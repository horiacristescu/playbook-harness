"""Deterministic cited judge for the arena mechanics canary."""

from __future__ import annotations

import json
import os
from pathlib import Path


def main() -> int:
    packet = json.loads(Path(os.environ["PLAYBOOK_ARENA_PACKET"]).read_text(encoding="utf-8"))
    available = {item["path"] for item in packet["artifacts"]}
    citations = [path for path in ("checks/summary.json", "worker/terminal.log", "worker/workspace.diff") if path in available]
    print(json.dumps({"schema": 1, "judge_id": "fixture-judge", "claims": [{"criterion_id": "audit-trail", "disposition": "pass" if len(citations) == 3 else "fail", "confidence": "high", "rationale": "The frozen check, terminal acknowledgment, and workspace change are all independently retained." if len(citations) == 3 else "The expected mechanics evidence is incomplete.", "citations": citations or [next(iter(available))]}]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
