"""Current-user fallback supervisor for the production observation lanes."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from schedule.runtime import JsonEventLogger, LockUnavailableError, ProcessLock

ROOT = Path(__file__).resolve().parents[1]


class ChildProcess(Protocol):
    def poll(self) -> int | None: ...


@dataclass(frozen=True)
class Lane:
    module: str
    interval_seconds: float
    stdout_path: Path
    stderr_path: Path


def default_lanes(root: Path = ROOT) -> tuple[Lane, ...]:
    runs = root / "runs"
    return (
        Lane(
            "schedule.modern_funnel",
            60,
            runs / "modern_funnel_supervisor.out.log",
            runs / "modern_funnel_supervisor.err.log",
        ),
        Lane(
            "schedule.postmarket",
            30 * 60,
            runs / "postmarket_supervisor.out.log",
            runs / "postmarket_supervisor.err.log",
        ),
    )


def launch_lane(lane: Lane) -> ChildProcess:
    lane.stdout_path.parent.mkdir(parents=True, exist_ok=True)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with (
        lane.stdout_path.open("ab") as stdout,
        lane.stderr_path.open("ab") as stderr,
    ):
        return subprocess.Popen(
            [sys.executable, "-m", lane.module],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
        )


def run_supervisor(
    *,
    lanes: tuple[Lane, ...],
    launcher: Callable[[Lane], ChildProcess] = launch_lane,
    logger: JsonEventLogger | None = None,
    max_seconds: float = 0.0,
    poll_seconds: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if not lanes or poll_seconds <= 0 or max_seconds < 0:
        raise ValueError("supervisor timing and lanes are invalid")
    if any(lane.interval_seconds <= 0 for lane in lanes):
        raise ValueError("lane intervals must be positive")
    event_logger = logger or JsonEventLogger(service="observation_supervisor")
    started = clock()
    next_due = {lane.module: started for lane in lanes}
    children: dict[str, ChildProcess] = {}
    event_logger.emit("supervisor_started", lanes=[lane.module for lane in lanes])

    while True:
        now = clock()
        for lane in lanes:
            child = children.get(lane.module)
            if child is not None:
                returncode = child.poll()
                if returncode is None:
                    continue
                event_logger.emit(
                    "lane_completed",
                    module=lane.module,
                    return_code=returncode,
                    level="info" if returncode == 0 else "error",
                )
                del children[lane.module]
            if now >= next_due[lane.module]:
                children[lane.module] = launcher(lane)
                next_due[lane.module] = now + lane.interval_seconds
                event_logger.emit("lane_started", module=lane.module)

        if max_seconds > 0 and now - started >= max_seconds:
            event_logger.emit("supervisor_stopped", reason="bounded_run")
            return
        wait_seconds = poll_seconds
        if max_seconds > 0:
            wait_seconds = min(wait_seconds, max_seconds - (now - started))
        sleep(max(wait_seconds, 0.001))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=ROOT / "runs/local-observation-supervisor.lock",
    )
    parser.add_argument("--max-seconds", type=float, default=0.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    try:
        with ProcessLock(args.lock_file):
            run_supervisor(
                lanes=default_lanes(),
                max_seconds=args.max_seconds,
                poll_seconds=args.poll_seconds,
            )
    except LockUnavailableError:
        print("local observation supervisor is already running")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
