from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from schedule.runtime import JsonEventLogger
from schedule.supervisor import Lane, run_supervisor


@dataclass
class FinishedProcess:
    returncode: int = 0

    def poll(self) -> int:
        return self.returncode


def test_supervisor_launches_each_lane_immediately_and_repeats_by_interval(
    tmp_path: Path,
) -> None:
    current = 0.0
    launched: list[str] = []
    lanes = (
        Lane("schedule.premarket", 5.0, tmp_path / "pre.out", tmp_path / "pre.err"),
        Lane("schedule.paper", 5.0, tmp_path / "paper.out", tmp_path / "paper.err"),
        Lane("schedule.postmarket", 30.0, tmp_path / "post.out", tmp_path / "post.err"),
    )

    def clock() -> float:
        return current

    def sleep(seconds: float) -> None:
        nonlocal current
        current += seconds

    def launcher(lane: Lane) -> FinishedProcess:
        launched.append(lane.module)
        return FinishedProcess()

    run_supervisor(
        lanes=lanes,
        launcher=launcher,
        logger=JsonEventLogger(stream=StringIO(), service="test-supervisor"),
        max_seconds=6.0,
        poll_seconds=1.0,
        clock=clock,
        sleep=sleep,
    )

    assert launched.count("schedule.premarket") == 2
    assert launched.count("schedule.paper") == 2
    assert launched.count("schedule.postmarket") == 1


def test_supervisor_does_not_overlap_a_running_lane(tmp_path: Path) -> None:
    current = 0.0
    launched = 0

    class RunningProcess:
        def poll(self) -> None:
            return None

    def clock() -> float:
        return current

    def sleep(seconds: float) -> None:
        nonlocal current
        current += seconds

    def launcher(lane: Lane) -> RunningProcess:
        nonlocal launched
        launched += 1
        return RunningProcess()

    run_supervisor(
        lanes=(Lane("schedule.paper", 1.0, tmp_path / "out", tmp_path / "err"),),
        launcher=launcher,
        logger=JsonEventLogger(stream=StringIO(), service="test-supervisor"),
        max_seconds=3.0,
        poll_seconds=0.5,
        clock=clock,
        sleep=sleep,
    )

    assert launched == 1
