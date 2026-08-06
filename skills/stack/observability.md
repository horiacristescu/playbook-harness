# Observability

You can't verify what you can't see. Build the feedback channel.

Channels (cheapest that answers the question):
- **A test** — "is this behavior correct?"
- **A log line** at the uncertain spot — structured, greppable
- **A visualization / small UI / dumped artifact** — when state is too big to read
- **`--verbose` / dry-run / `--explain`** — show reasoning, not just result
- **Playwright e2e** — to actually see a UI work

Make surfaces machine- and human-readable:
- **`--json`** on every CLI (table by default)
- **Stable, documented exit codes**
- **Typed DTOs** shared across CLI/API/UI
- **Stable API error shapes/codes**
- **State as JSON/JSONL on disk** (`cat`/`jq`-able); **SQLite** for queryable state
- **Logs to a known `logs/<id>.log`**
- **Structured logging**
- **`.mission-control.yaml`** — a machine-readable project descriptor (at minimum `version` + a `project` description; extend with services/URLs/probes/logs as the project grows) so a workspace tool can discover how to observe it
- **`app.sh status` + `logs`** — read-only "what's running"
- **A health probe** (`/health` or a `check` verb)
- **A preflight script** — before a server/batch job starts, validate required env vars, API keys, reachable endpoints, and config; fail loud with the missing item named, so failure is at second 0, not mid-run
- **A "dev loop" section** in AGENTS.md — set up, change, see the result
