"""Bounded subprocess capture for untrusted arena checks and judges."""

from __future__ import annotations

import os
import resource
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    stdout_overflow: bool
    stderr_overflow: bool
    timed_out: bool = False
    environment_error: str | None = None


def _limit_output(file_bytes: int):
    def apply_limit() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))

    return apply_limit


def _read_capture(path: Path, limit: int) -> tuple[bytes, bool]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        return handle.read(limit), size > limit


def run_bounded(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    per_stream_limit: int,
) -> BoundedProcessResult:
    """Run argv with each captured stream capped before it reaches Python memory."""
    if per_stream_limit < 1:
        raise ValueError("per_stream_limit must be positive")
    with tempfile.TemporaryDirectory(prefix="pb-arena-output-") as raw:
        root = Path(raw)
        stdout_path = root / "stdout"
        stderr_path = root / "stderr"
        try:
            with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
                process = subprocess.Popen(
                    list(argv),
                    cwd=cwd,
                    env=dict(env),
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    start_new_session=True,
                    preexec_fn=_limit_output(per_stream_limit + 1),
                )
                timed_out = False
                try:
                    returncode = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    returncode = process.wait()
        except OSError as exc:
            return BoundedProcessResult(None, b"", b"", False, False, environment_error=str(exc))
        stdout, stdout_overflow = _read_capture(stdout_path, per_stream_limit)
        stderr, stderr_overflow = _read_capture(stderr_path, per_stream_limit)
        return BoundedProcessResult(returncode, stdout, stderr, stdout_overflow, stderr_overflow, timed_out=timed_out)
