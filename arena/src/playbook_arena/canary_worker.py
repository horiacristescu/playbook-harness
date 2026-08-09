"""Deterministic interactive worker for the shipped arena mechanics canary."""

from __future__ import annotations

import sys
from pathlib import Path


MESSAGE = "Create the deterministic arena marker and acknowledge completion."


def main() -> int:
    print("READY arena-canary", flush=True)
    message = sys.stdin.readline().rstrip("\r\n")
    if message != MESSAGE:
        print("ERROR unexpected controller message", flush=True)
        return 2
    Path("arena-canary.marker").write_text("created by deterministic fixture\n", encoding="utf-8")
    print("ACK arena-canary", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
