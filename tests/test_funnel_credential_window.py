import json
import sys
from datetime import UTC, date, datetime, time, tzinfo
from pathlib import Path
from typing import Self
from zoneinfo import ZoneInfo

import pytest

from schedule import modern_funnel as funnel
from schedule.modern_funnel import FunnelStage, FunnelTickStatus, run_tick

BEIJING = ZoneInfo("Asia/Shanghai")
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


def test_summer_first_wave_before_2100_beijing_never_creates_ledger(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    ledger = tmp_path / "uncreated" / "funnel.sqlite3"
    result = run_tick(
        ledger_path=ledger,
        executor=executor,
        now_utc=datetime(2026, 9, 3, 20, 59, tzinfo=BEIJING).astimezone(UTC),
        first_wave_not_before_beijing=time(21),
    )
    assert result.status is FunnelTickStatus.NOT_DUE
    assert executor.calls == []
    assert not ledger.parent.exists()


@pytest.mark.parametrize("month,day,et_hour", [(9, 3, 9), (12, 3, 8)])
def test_first_wave_starts_at_2100_beijing_in_summer_and_winter(
    tmp_path: Path,
    month: int,
    day: int,
    et_hour: int,
) -> None:
    current = datetime(2026, month, day, 21, 0, tzinfo=BEIJING)
    assert current.astimezone(EASTERN).hour == et_hour
    executor = RecordingExecutor()
    result = run_tick(
        ledger_path=tmp_path / "funnel.sqlite3",
        executor=executor,
        now_utc=current,
        first_wave_not_before_beijing=time(21),
    )
    assert result.status is FunnelTickStatus.SUCCEEDED
    assert executor.calls == [(FunnelStage.FIRST_WAVE, date(2026, month, day), False)]


def test_default_none_keeps_original_first_wave_window(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    result = run_tick(
        ledger_path=tmp_path / "funnel.sqlite3",
        executor=executor,
        now_utc=datetime(2026, 9, 3, 20, 0, tzinfo=BEIJING),
    )
    assert result.status is FunnelTickStatus.SUCCEEDED
    assert executor.calls == [(FunnelStage.FIRST_WAVE, date(2026, 9, 3), False)]


def test_xnys_holiday_takes_priority_over_first_wave_gate(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    ledger = tmp_path / "uncreated" / "funnel.sqlite3"
    result = run_tick(
        ledger_path=ledger,
        executor=executor,
        now_utc=datetime(2026, 9, 7, 20, 59, tzinfo=BEIJING),
        first_wave_not_before_beijing=time(21),
    )
    assert result.status is FunnelTickStatus.NOT_TRADING_DAY
    assert executor.calls == []
    assert not ledger.parent.exists()


@pytest.mark.parametrize(
    "hour,minute,target,resume",
    [
        (9, 25, FunnelStage.SECOND_WAVE, False),
        (9, 35, FunnelStage.OPEN_CONFIRMATION, False),
        (12, 0, FunnelStage.OPEN_CONFIRMATION, True),
        (15, 59, FunnelStage.OPEN_CONFIRMATION, True),
    ],
)
def test_first_wave_gate_does_not_block_later_stages_or_beijing_midnight_recovery(
    tmp_path: Path,
    hour: int,
    minute: int,
    target: FunnelStage,
    resume: bool,
) -> None:
    executor = RecordingExecutor()
    ledger = tmp_path / "funnel.sqlite3"
    trade_date = date(2026, 9, 3)
    prerequisites = [(8, 0)]
    if target is FunnelStage.OPEN_CONFIRMATION:
        prerequisites.append((9, 25))
    if resume:
        prerequisites.append((9, 35))
    for prior_hour, prior_minute in prerequisites:
        run_tick(
            ledger_path=ledger,
            executor=executor,
            now_utc=datetime(2026, 9, 3, prior_hour, prior_minute, tzinfo=EASTERN),
        )
    current = datetime(2026, 9, 3, hour, minute, tzinfo=EASTERN)
    if resume:
        assert current.astimezone(BEIJING).date() == date(2026, 9, 4)
    gate = time(21) if resume else time(23, 59)
    assert current.astimezone(BEIJING).time() < gate

    result = run_tick(
        ledger_path=ledger,
        executor=executor,
        now_utc=current,
        first_wave_not_before_beijing=gate,
    )
    expected = (
        FunnelTickStatus.SUCCEEDED
        if target is FunnelStage.SECOND_WAVE
        else FunnelTickStatus.MONITORING
    )
    assert result.status is expected
    assert executor.calls[-1] == (target, trade_date, resume)


def test_cli_passes_first_wave_clock_gate_to_real_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    current = datetime(2026, 9, 3, 20, 59, tzinfo=BEIJING)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(current.timestamp(), tz=tz)

    executor = RecordingExecutor()
    ledger = tmp_path / "uncreated" / "funnel.sqlite3"
    monkeypatch.setattr(funnel, "datetime", Clock)
    monkeypatch.setattr(funnel, "ProductionFunnelExecutor", lambda **_: executor)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "funnel",
            "--ledger-path",
            str(ledger),
            "--first-wave-not-before-beijing",
            "21:00",
        ],
    )
    assert funnel.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "not_due"
    assert executor.calls == []
    assert not ledger.parent.exists()


@pytest.mark.parametrize(
    "value",
    ["24:00", "21:60", "9:00", "21", "21:00:00", "21:00+08:00", "", "２１:００"],
)
def test_cli_rejects_invalid_not_before_times_without_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    executor = RecordingExecutor()
    ledger = tmp_path / "uncreated" / "funnel.sqlite3"
    monkeypatch.setattr(funnel, "ProductionFunnelExecutor", lambda **_: executor)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "funnel",
            "--ledger-path",
            str(ledger),
            "--first-wave-not-before-beijing",
            value,
        ],
    )
    with pytest.raises(SystemExit) as caught:
        funnel.main()
    assert caught.value.code == 2
    assert "time must use HH:MM" in capsys.readouterr().err
    assert executor.calls == []
    assert not ledger.parent.exists()
