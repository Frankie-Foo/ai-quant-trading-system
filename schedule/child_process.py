"""Bounded, shell-free execution seam for scheduler child processes."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ChildProcessResult:
    """Stable scheduler-facing result without exposing ``CompletedProcess``."""

    return_code: int
    stdout: str
    stderr: str
    elapsed_ms: int


def run_child(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    capture_output: bool = True,
) -> ChildProcessResult:
    """Run one trusted child command with an explicit timeout and no shell."""

    normalized = tuple(str(part) for part in command)
    if not normalized or any(not part for part in normalized):
        raise ValueError("child command must contain non-empty arguments")
    if timeout_seconds <= 0:
        raise ValueError("child timeout must be positive")

    from time import monotonic

    started = monotonic()
    completed = subprocess.run(
        normalized,
        cwd=cwd,
        capture_output=capture_output,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return ChildProcessResult(
        return_code=completed.returncode,
        stdout=completed.stdout if isinstance(completed.stdout, str) else "",
        stderr=completed.stderr if isinstance(completed.stderr, str) else "",
        elapsed_ms=round((monotonic() - started) * 1000),
    )
