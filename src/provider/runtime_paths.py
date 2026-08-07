"""Provider-neutral machine cache paths for the Harness runtime.

These locations are mutable machine state.  Project task and integration state
is deliberately *not* routed here; it remains under the selected project root.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


APPLICATION_DIR = "playbook-harness"


def _absolute_xdg(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else None


def user_cache_dir(
    *, env: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    values = os.environ if env is None else env
    base = _absolute_xdg(values.get("XDG_CACHE_HOME"))
    if base is not None:
        return base / APPLICATION_DIR
    home_dir = Path.home() if home is None else home
    return home_dir / ".cache" / APPLICATION_DIR
