# Python

- **uv** — package/env manager (init, add, lock, run)
- **ruff** — format + lint, autofix
- **mypy** — type checker
- **pytest** — tests
- **FastAPI** + **uvicorn** — APIs
- **Pydantic v2** — validation at boundaries ⚑
- **dataclasses** — internal models
- **pathlib** — paths
- **logging** (JSON formatter) — logs
- **sqlite3 / stdlib** — small storage; **SQLAlchemy** only for a real data model
- **argparse** — tool CLIs; **typer** when the CLI is a product
- **Polars** — new dataframe pipelines

Layout: `src/<pkg>/`, `tests/`, config in `pyproject.toml`.

⚑ Pin in AGENTS.md (post-cutoff): **uv**, **ruff** (format+lint), **Pydantic v2**.
