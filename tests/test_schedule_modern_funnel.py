import json
import sqlite3
import subprocess
import traceback
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from operations.autonomous_selection_handoff import create_open_confirmation
from schedule.modern_funnel import (
    CompletedStageProcess,
    FunnelStage,
    FunnelTickResult,
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

    def can_resume(self, trade_date: date, *, now_utc: datetime) -> bool:
        return False

    def execute(
        self,
        stage: FunnelStage,
        trade_date: date,
        *,
        resume_only: bool = False,
    ) -> dict[str, str]:
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


def test_production_executor_preserves_the_sanitized_failure_reason(tmp_path: Path) -> None:
    def runner(command: list[str], **_: object) -> CompletedStageProcess:
        del command

        class Completed:
            returncode = 1
            stdout = ""
            stderr = "Traceback omitted\nRuntimeError: Paper startup failed\n"

        return Completed()

    executor = ProductionFunnelExecutor(root=tmp_path, runner=runner)
    with pytest.raises(RuntimeError, match="Paper startup failed"):
        executor.execute(FunnelStage.OPEN_CONFIRMATION, TRADE_DATE)


def test_september_third_wave_pending_handoff_can_resume_without_selection(tmp_path: Path) -> None:
    ledger = tmp_path / "funnel.sqlite3"
    commands: list[list[str]] = []
    started = False
    day_root = tmp_path / "runs" / "autonomous" / "2026-09-03"
    day_root.mkdir(parents=True)
    plan_path = day_root / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    create_open_confirmation(
        confirmation_path=day_root / "open_confirmation.json",
        config_path=plan_path,
        trade_date=date(2026, 9, 3),
        selection_snapshot_id="frozen-0903",
        candidate_pool=("PASS",),
        feishu_record_ids=("rec-0903",),
        livermore_message_id="msg-0903",
        strategy_version="modern-h15.v1",
        generated_at_utc=datetime(2026, 9, 3, 13, 35, tzinfo=UTC),
    )

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        receipt: dict[str, object] = {"ok": True, "receipt_id": "published-0903"}
        if "open_confirmation" in command:
            receipt.update(
                symbols="PASS",
                authorization_id="auth-0903",
                paper_started=started,
                feishu_record_ids="rec-0903",
                livermore_message_id="msg-0903",
            )
        return subprocess.CompletedProcess(command, 0, json.dumps(receipt), "")

    executor = ProductionFunnelExecutor(root=tmp_path, runner=runner)

    def tick(hour: int, minute: int) -> FunnelTickResult:
        return run_tick(
            ledger_path=ledger,
            executor=executor,
            now_utc=datetime(2026, 9, 3, hour, minute, tzinfo=EASTERN).astimezone(UTC),
        )

    tick(8, 0)
    tick(9, 25)
    pending = tick(9, 35)
    assert pending.status.value == "handoff_pending"
    # Replay the old 9/3 bug: an eligible, unstarted receipt was locked as succeeded.
    with sqlite3.connect(ledger) as connection:
        connection.execute(
            "UPDATE funnel_runs SET status='succeeded' WHERE stage='open_confirmation'"
        )
    started = True
    resumed = tick(10, 0)
    assert resumed.status.value == "monitoring"
    assert "--resume-only" in commands[-1]
    assert tick(10, 1).status.value == "monitoring"
    assert tick(15, 0).status is FunnelTickStatus.MONITORING
    assert tick(16, 0).status is FunnelTickStatus.NOT_DUE
    assert len(commands) == 6


@pytest.mark.parametrize("timeout", [False, True])
def test_stage_process_failures_never_copy_secrets_into_ledger_or_traceback(
    tmp_path: Path,
    timeout: bool,
) -> None:
    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if timeout:
            raise subprocess.TimeoutExpired(["SECRET"], 3600, "SECRET", "SECRET")
        return subprocess.CompletedProcess(command, 1, "SECRET", "RuntimeError: SECRET")

    executor = ProductionFunnelExecutor(root=tmp_path, runner=runner)
    with pytest.raises(RuntimeError) as caught:
        executor.execute(FunnelStage.OPEN_CONFIRMATION, TRADE_DATE)
    assert "SECRET" not in "".join(traceback.format_exception(caught.value))


def test_stage_failure_keeps_only_safe_feishu_code_and_type(tmp_path: Path) -> None:
    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            "SECRET",
            "SECRET traceback\noperations.feishu_base.FeishuCliError: "
            "lark-cli failed (type=rate_limit, code=99991400, exit_code=1)",
        )

    with pytest.raises(RuntimeError, match="type=rate_limit, code=99991400") as caught:
        ProductionFunnelExecutor(root=tmp_path, runner=runner).execute(
            FunnelStage.OPEN_CONFIRMATION,
            TRADE_DATE,
        )
    assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize("artifact", ["missing", "decision_only", "invalid_authorization"])
def test_failed_open_without_authorization_preserves_original_error_after_window(
    tmp_path: Path,
    artifact: str,
) -> None:
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "open_confirmation" in command:
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "operations.feishu_base.FeishuCliError: "
                "lark-cli failed (type=rate_limit, code=99991400, exit_code=1)",
            )
        return subprocess.CompletedProcess(command, 0, '{"ok": true, "receipt_id": "prior"}', "")

    executor = ProductionFunnelExecutor(root=tmp_path, runner=runner)
    ledger = tmp_path / "funnel.sqlite3"
    if artifact != "missing":
        day_root = tmp_path / "runs" / "autonomous" / TRADE_DATE.isoformat()
        day_root.mkdir(parents=True)
        name = "open_decision.json" if artifact == "decision_only" else "open_confirmation.json"
        (day_root / name).write_text('{"candidates": [{"symbol": "PASS"}]}', encoding="utf-8")
    for hour, minute in ((8, 0), (9, 25), (9, 35)):
        result = run_tick(ledger_path=ledger, executor=executor, now_utc=_utc(hour, minute))
    assert result.status is FunnelTickStatus.FAILED
    with sqlite3.connect(ledger) as connection:
        before = connection.execute(
            "SELECT * FROM funnel_runs WHERE stage='open_confirmation'"
        ).fetchone()
    for hour, minute in ((9, 45), (10, 0), (15, 30)):
        outside = run_tick(ledger_path=ledger, executor=executor, now_utc=_utc(hour, minute))
        assert outside.status is FunnelTickStatus.NOT_DUE
    with sqlite3.connect(ledger) as connection:
        after = connection.execute(
            "SELECT * FROM funnel_runs WHERE stage='open_confirmation'"
        ).fetchone()
    assert after == before
    assert "99991400" in str(after)
    assert len(commands) == 3


def test_no_trade_remains_terminal_and_scheduler_ignores_midnight_and_non_sessions(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "funnel.sqlite3"
    executor = FakeExecutor()
    for hour, minute in ((8, 0), (9, 25), (9, 35)):
        assert (
            run_tick(
                ledger_path=ledger,
                executor=executor,
                now_utc=_utc(hour, minute),
            ).status
            is FunnelTickStatus.SUCCEEDED
        )
    with sqlite3.connect(ledger) as connection:
        original = connection.execute("SELECT * FROM funnel_runs ORDER BY stage").fetchall()
    for current, expected in (
        (_utc(0, 0), FunnelTickStatus.NOT_DUE),
        (_utc(10, 0), FunnelTickStatus.NOT_DUE),
        (_utc(15, 59), FunnelTickStatus.NOT_DUE),
        (_utc(10, 0, day=23), FunnelTickStatus.NOT_TRADING_DAY),
        (datetime(2026, 9, 7, 14, 0, tzinfo=UTC), FunnelTickStatus.NOT_TRADING_DAY),
    ):
        assert (
            run_tick(
                ledger_path=ledger,
                executor=executor,
                now_utc=current,
            ).status
            is expected
        )
    with sqlite3.connect(ledger) as connection:
        assert connection.execute("SELECT * FROM funnel_runs ORDER BY stage").fetchall() == original
    assert len(executor.calls) == 3
