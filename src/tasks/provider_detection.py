"""Read-only supported-agent discovery for project reconciliation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


Version = tuple[int, ...]


class DetectionStatus(str, Enum):
    SUPPORTED = "supported"
    ABSENT = "absent"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    executables: tuple[str, ...]
    minimum_version: Version
    version_args: tuple[str, ...] = ("--version",)
    fallback_locations: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderDetection:
    name: str
    status: DetectionStatus
    executable: Path | None = None
    version: Version | None = None
    raw_version: str = ""
    detail: str = ""


ProbeRunner = Callable[
    [tuple[str, ...], Mapping[str, str], Path, float], subprocess.CompletedProcess[str]
]


PROVIDER_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "claude",
        ("claude",),
        (2, 1, 220),
        fallback_locations=(".local/bin/claude", ".claude/local/claude"),
    ),
    ProviderSpec(
        "codex",
        ("codex",),
        (0, 125, 0),
        fallback_locations=(".local/bin/codex",),
    ),
    ProviderSpec(
        "antigravity",
        ("agy", "antigravity"),
        (1, 1, 10),
        fallback_locations=(".local/bin/agy",),
    ),
    ProviderSpec(
        "pi",
        ("pi",),
        (0, 73, 0),
        fallback_locations=(".local/bin/pi",),
    ),
    ProviderSpec(
        "omp",
        ("omp",),
        (17, 2, 9),
        fallback_locations=(".local/bin/omp",),
    ),
)


_VERSION_RE = re.compile(r"(?<![0-9])([0-9]+(?:\.[0-9]+)+)(?![0-9])")
_PROBE_ENV_ALLOWLIST = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    # Windows process startup needs these even though provider state does not.
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
)


def detect_providers(
    *,
    specs: Iterable[ProviderSpec] = PROVIDER_SPECS,
    search_path: str | None = None,
    home: Path | None = None,
    runner: ProbeRunner | None = None,
    timeout: float = 3.0,
    environ: Mapping[str, str] | None = None,
) -> tuple[ProviderDetection, ...]:
    """Detect provider binaries and versions without model/auth requests.

    Version commands are a reviewed trust boundary, not hostile-code
    containment. They receive a disposable HOME/XDG environment and cwd so
    ordinary first-run writes cannot reach real provider state.
    """
    values = os.environ if environ is None else environ
    path_value = values.get("PATH", os.defpath) if search_path is None else search_path
    home_dir = Path.home() if home is None else home
    run = _run_probe if runner is None else runner
    detections: list[ProviderDetection] = []

    with tempfile.TemporaryDirectory(prefix="playbook-provider-probe-") as temporary:
        probe_root = Path(temporary)
        probe_env = _probe_environment(values, path_value, probe_root)
        for spec in specs:
            executable = _find_executable(spec, path_value, home_dir)
            if executable is None:
                detections.append(
                    ProviderDetection(spec.name, DetectionStatus.ABSENT, detail="not installed")
                )
                continue
            argv = (str(executable), *spec.version_args)
            try:
                result = run(argv, probe_env, probe_root, timeout)
            except subprocess.TimeoutExpired:
                detections.append(
                    ProviderDetection(
                        spec.name,
                        DetectionStatus.UNSUPPORTED,
                        executable,
                        detail="version probe timed out",
                    )
                )
                continue
            except OSError as exc:
                detections.append(
                    ProviderDetection(
                        spec.name,
                        DetectionStatus.UNSUPPORTED,
                        executable,
                        detail=f"version probe failed: {exc}",
                    )
                )
                continue
            raw = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
            version = parse_version(raw)
            if result.returncode != 0:
                detail = f"version probe exited {result.returncode}"
                status = DetectionStatus.UNSUPPORTED
            elif version is None:
                detail = "version output was not understood"
                status = DetectionStatus.UNSUPPORTED
            elif _version_at_least(version, spec.minimum_version):
                detail = "installed and supported"
                status = DetectionStatus.SUPPORTED
            else:
                detail = (
                    f"version {_format_version(version)} is below minimum "
                    f"{_format_version(spec.minimum_version)}"
                )
                status = DetectionStatus.UNSUPPORTED
            detections.append(
                ProviderDetection(spec.name, status, executable, version, raw, detail)
            )
    return tuple(detections)


def parse_version(output: str) -> Version | None:
    match = _VERSION_RE.search(output)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _find_executable(spec: ProviderSpec, search_path: str, home: Path) -> Path | None:
    for name in spec.executables:
        found = shutil.which(name, path=search_path)
        if found:
            return Path(found).resolve()
    for relative in spec.fallback_locations:
        candidate = home / relative
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    return None


def _probe_environment(
    environ: Mapping[str, str], search_path: str, root: Path
) -> dict[str, str]:
    result = {
        key: environ[key]
        for key in _PROBE_ENV_ALLOWLIST
        if key in environ
    }
    result.update(
        {
            "PATH": search_path,
            "HOME": str(root / "home"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "TMPDIR": str(root / "tmp"),
            "TMP": str(root / "tmp"),
            "TEMP": str(root / "tmp"),
        }
    )
    for key in (
        "HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "TMPDIR",
    ):
        Path(result[key]).mkdir(parents=True, exist_ok=True)
    return result


def _run_probe(
    argv: tuple[str, ...], env: Mapping[str, str], cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        env=dict(env),
        cwd=cwd,
        timeout=timeout,
        capture_output=True,
        text=True,
        check=False,
    )


def _version_at_least(actual: Version, minimum: Version) -> bool:
    width = max(len(actual), len(minimum))
    return actual + (0,) * (width - len(actual)) >= minimum + (0,) * (width - len(minimum))


def _format_version(version: Version) -> str:
    return ".".join(str(part) for part in version)
