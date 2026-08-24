from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from schedule.modern_funnel import (
    CompletedStageProcess,
    FunnelStage,
    FunnelTickStatus,
    ProductionFunnelExecutor,
    run_tick,
)

EASTERN = ZoneInfo("America/New_York")
TRADE_DATE = date(2026, 8, 24)


class FakeExecutor:
    def __init__(self, *, fail_once: FunnelStage | None = None) -> None:
        self.calls: list[FunnelStage] = []
        self.fail_once = fail_once

    def execute(self, stage: FunnelStage, trade_date: date) -> dict[str, str]:
        assert trade_date == TRADE_DATE
        self.calls.append(stage)
        if self.fail_once == stage:
            self.fail_once = None
            raise RuntimeError("temporary dependency failure")
        return {"receipt_id": f"{stage.value}-receipt"}


def _utc(hour: int, minute: int, *, day: int = 24) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=EASTERN).astimezone(UTC)


def test_funnel_runs_each_stage_once_in_dependency_order(tmp_path: Path) -> None:
    ledger = tmp_path / "funnel.sqlite3"
    executor = FakeExecutor()

    first = run_tick(ledger_path=ledger, executor=executor, now_utc=_utc(8, 0))
    duplicate = run_tick(ledger_path=ledger, executor=executor, now_utc=_utc(8, 1))
    second = run_tick(ledger_path=ledger, executor=executor, now_utc=_utc(9, 25))
    open_confirmation = run_tick(
        ledger_path=ledger,
        executor=executor,
        now_utc=_utc(9, 35),
    )

    assert first.status is FunnelTickStatus.SUCCEEDED
    assert duplicate.status is FunnelTickStatus.ALREADY_SUCCEEDED
    assert second.status is FunnelTickStatus.SUCCEEDED
    assert open_confirmation.status is FunnelTickStatus.SUCCEEDED
    assert executor.calls == [
        FunnelStage.FIRST_WAVE,
        FunnelStage.SECOND_WAVE,
        FunnelStage.OPEN_CONFIRMATION,
    ]


def test_failed_stage_retries_only_inside_its_window(tmp_path: Path) -> None:
    ledger = tmp_path / "funnel.sqlite3"
    executor = FakeExecutor(fail_once=FunnelStage.SECOND_WAVE)
    run_tick(ledger_path=ledger, executor=executor, now_utc=_utc(8, 0))

    failed = run_tick(ledger_path=ledger, executor=executor, now_utc=_utc(9, 25))
    retried = run_tick(ledger_path=ledger, executor=executor, now_utc=_utc(9, 26))
    outside = run_tick(ledger_path=ledger, executor=executor, now_utc=_utc(9, 30))

    assert failed.status is FunnelTickStatus.FAILED
    assert retried.status is FunnelTickStatus.SUCCEEDED
    assert outside.status is FunnelTickStatus.NOT_DUE
    assert executor.calls.count(FunnelStage.SECOND_WAVE) == 2


def test_missing_prerequisite_never_runs_a_later_stage(tmp_path: Path) -> None:
    executor = FakeExecutor()
    result = run_tick(
        ledger_path=tmp_path / "funnel.sqlite3",
        executor=executor,
        now_utc=_utc(9, 25),
    )
    assert result.status is FunnelTickStatus.PREREQUISITE_MISSING
    assert executor.calls == []


def test_funnel_does_nothing_outside_windows_or_on_xnys_holiday(tmp_path: Path) -> None:
    executor = FakeExecutor()
    outside = run_tick(
        ledger_path=tmp_path / "weekday.sqlite3",
        executor=executor,
        now_utc=_utc(9, 31),
    )
    weekend = run_tick(
        ledger_path=tmp_path / "weekend.sqlite3",
        executor=executor,
        now_utc=_utc(8, 0, day=23),
    )
    assert outside.status is FunnelTickStatus.NOT_DUE
    assert weekend.status is FunnelTickStatus.NOT_TRADING_DAY
    assert executor.calls == []


def test_open_confirmation_window_ends_at_0945(tmp_path: Path) -> None:
    ledger = tmp_path / "funnel.sqlite3"
    executor = FakeExecutor()
    run_tick(ledger_path=ledger, executor=executor, now_utc=_utc(8, 0))
    run_tick(ledger_path=ledger, executor=executor, now_utc=_utc(9, 25))
    result = run_tick(ledger_path=ledger, executor=executor, now_utc=_utc(9, 45))
    assert result.status is FunnelTickStatus.NOT_DUE
    assert FunnelStage.OPEN_CONFIRMATION not in executor.calls


def test_production_executor_requires_a_json_success_receipt(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_: object) -> CompletedStageProcess:
        calls.append(command)

        class Completed:
            returncode = 0
            stdout = '{"ok": true, "receipt_id": "first-1"}\n'
            stderr = ""

        return Completed()

    executor = ProductionFunnelExecutor(root=tmp_path, runner=runner)
    receipt = executor.execute(FunnelStage.FIRST_WAVE, TRADE_DATE)

    assert receipt["receipt_id"] == "first-1"
    assert calls[0][-4:] == [
        "--stage",
        "first_wave",
        "--trade-date",
        TRADE_DATE.isoformat(),
    ]


def test_production_executor_rejects_empty_or_failed_stage_receipt(tmp_path: Path) -> None:
    def runner(command: list[str], **_: object) -> CompletedStageProcess:
        del command

        class Completed:
            returncode = 0
            stdout = '{"ok": false}\n'
            stderr = ""

        return Completed()

    executor = ProductionFunnelExecutor(root=tmp_path, runner=runner)
    with pytest.raises(RuntimeError, match="did not produce a success receipt"):
        executor.execute(FunnelStage.SECOND_WAVE, TRADE_DATE)
