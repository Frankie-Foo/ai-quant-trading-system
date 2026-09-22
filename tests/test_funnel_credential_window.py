import json
import sys
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from typing import Self
from zoneinfo import ZoneInfo

import pytest

from schedule import modern_funnel as funnel
from schedule.modern_funnel import FunnelStage, FunnelTickStatus, run_tick

EASTERN = ZoneInfo("America/New_York")


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[FunnelStage, date, bool]] = []

    def execute(
        self,
        stage: FunnelStage,
        trade_date: date,
        *,
        resume_only: bool = False,
    ) -> dict[str, str]:
        self.calls.append((stage, trade_date, resume_only))
        receipt = {"receipt_id": stage.value}
        if stage is FunnelStage.OPEN_CONFIRMATION:
            receipt.update(symbols="PASS", authorization_id="frozen", paper_started="true")
        return receipt

    def can_resume(self, trade_date: date, *, now_utc: datetime) -> bool:
        return True


def test_first_wave_before_0830_et_never_creates_ledger(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    ledger = tmp_path / "uncreated" / "funnel.sqlite3"
    result = run_tick(
        ledger_path=ledger,
        executor=executor,
        now_utc=datetime(2026, 9, 3, 8, 29, tzinfo=EASTERN).astimezone(UTC),
    )
    assert result.status is FunnelTickStatus.NOT_DUE
    assert executor.calls == []
    assert not ledger.parent.exists()


@pytest.mark.parametrize("month,day", [(9, 3), (12, 3)])
def test_first_wave_starts_at_0830_et_in_summer_and_winter(
    tmp_path: Path,
    month: int,
    day: int,
) -> None:
    current = datetime(2026, month, day, 8, 30, tzinfo=EASTERN)
    executor = RecordingExecutor()
    result = run_tick(ledger_path=tmp_path / "funnel.sqlite3", executor=executor, now_utc=current)
    assert result.status is FunnelTickStatus.SUCCEEDED
    assert executor.calls == [(FunnelStage.FIRST_WAVE, date(2026, month, day), False)]


def test_xnys_holiday_takes_priority_over_first_wave(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    ledger = tmp_path / "uncreated" / "funnel.sqlite3"
    result = run_tick(
        ledger_path=ledger,
        executor=executor,
        now_utc=datetime(2026, 9, 7, 8, 30, tzinfo=EASTERN),
    )
    assert result.status is FunnelTickStatus.NOT_TRADING_DAY
    assert executor.calls == []
    assert not ledger.parent.exists()


@pytest.mark.parametrize(
    "hour,minute,target,resume",
    [
        (9, 0, FunnelStage.SECOND_WAVE, False),
        (9, 30, FunnelStage.FINAL_RANK, False),
        (9, 35, FunnelStage.OPEN_CONFIRMATION, False),
        (12, 0, FunnelStage.OPEN_CONFIRMATION, True),
        (15, 59, FunnelStage.OPEN_CONFIRMATION, True),
    ],
)
def test_exchange_clock_runs_later_stages_and_recovery(
    tmp_path: Path,
    hour: int,
    minute: int,
    target: FunnelStage,
    resume: bool,
) -> None:
    executor = RecordingExecutor()
    ledger = tmp_path / "funnel.sqlite3"
    trade_date = date(2026, 9, 3)
    prerequisites = [(8, 30)]
    if target in {FunnelStage.FINAL_RANK, FunnelStage.OPEN_CONFIRMATION}:
        prerequisites.append((9, 0))
    if target is FunnelStage.OPEN_CONFIRMATION:
        prerequisites.append((9, 30))
    if resume:
        prerequisites.append((9, 35))
    for prior_hour, prior_minute in prerequisites:
        run_tick(
            ledger_path=ledger,
            executor=executor,
            now_utc=datetime(2026, 9, 3, prior_hour, prior_minute, tzinfo=EASTERN),
        )
    current = datetime(2026, 9, 3, hour, minute, tzinfo=EASTERN)
    result = run_tick(ledger_path=ledger, executor=executor, now_utc=current)
    expected = (
        FunnelTickStatus.MONITORING
        if target is FunnelStage.OPEN_CONFIRMATION
        else FunnelTickStatus.SUCCEEDED
    )
    assert result.status is expected
    assert executor.calls[-1] == (target, trade_date, resume)


def test_cli_uses_exchange_clock_for_first_wave(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    current = datetime(2026, 9, 3, 8, 30, tzinfo=EASTERN)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(current.timestamp(), tz=tz)

    executor = RecordingExecutor()
    ledger = tmp_path / "funnel.sqlite3"
    monkeypatch.setattr(funnel, "datetime", Clock)
    monkeypatch.setattr(funnel, "ProductionFunnelExecutor", lambda **_: executor)
    monkeypatch.setattr(sys, "argv", ["funnel", "--ledger-path", str(ledger)])
    assert funnel.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"
    assert executor.calls == [(FunnelStage.FIRST_WAVE, date(2026, 9, 3), False)]
