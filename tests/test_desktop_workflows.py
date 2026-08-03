from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from execution.alpaca_sip_stream import SipQuote
from execution.sip_store import SipEventStore
from operations.desktop_workflows import (
    CommandSpec,
    DesktopWorkflowManager,
    SelectionSipMonitor,
)


class _RecordingRunner:
    def __init__(self) -> None:
        self.commands: list[CommandSpec] = []

    def run(
        self,
        command: CommandSpec,
        *,
        log_path: Path,
        on_output: Callable[[str], None] | None = None,
    ) -> None:
        self.commands.append(command)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"completed {command.module}\n", encoding="utf-8")


class _ProgressRunner:
    def __init__(self) -> None:
        self.reported = threading.Event()
        self.release = threading.Event()

    def run(
        self,
        command: CommandSpec,
        *,
        log_path: Path,
        on_output: Callable[[str], None] | None = None,
    ) -> None:
        assert on_output is not None
        on_output('{"progress":"5/100","trade_date":"2025-01-08"}')
        self.reported.set()
        self.release.wait(2)


class _RecordingMonitor:
    def run(
        self,
        *,
        stop_event: threading.Event,
        trade_date: date,
        data_root: Path,
        runs_root: Path,
    ) -> dict[str, object]:
        stop_event.wait(0.05)
        return {"events_stored": 7, "symbols": ["AAPL"]}


class _DelayedStream:
    async def events(self) -> AsyncGenerator[SipQuote, None]:
        await asyncio.sleep(1.2)
        yield SipQuote(
            symbol="BRKR",
            ts_utc=datetime(2026, 7, 31, 15, 30, tzinfo=UTC),
            bid_price=65.0,
            bid_size=10,
            ask_price=65.1,
            ask_size=12,
            provenance="test.delayed.quote",
        )
        while True:
            await asyncio.sleep(10)


def test_desktop_workflow_sync_persists_progress_and_is_restart_readable(
    tmp_path: Path,
) -> None:
    runner = _RecordingRunner()
    manager = DesktopWorkflowManager(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        runner=runner,
    )

    accepted = manager.submit("sync_data", trade_date=date(2026, 7, 31))
    manager.wait_for_idle(timeout_seconds=5)
    status = manager.status()
    restored = DesktopWorkflowManager(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        runner=_RecordingRunner(),
    ).status()

    assert accepted["accepted"] is True
    assert status["active_job"] is None
    assert status["latest_job"]["status"] == "complete"
    assert status["latest_job"]["completed_steps"] == 3
    assert [command.module for command in runner.commands] == [
        "data_plane.cli",
        "scripts.backfill_massive_reference_weekly",
        "scripts.backfill_massive_news",
    ]
    assert restored["latest_job"] == status["latest_job"]
    assert status["orders_submitted"] == 0


def test_desktop_workflow_selection_runs_the_existing_deterministic_pipeline(
    tmp_path: Path,
) -> None:
    runner = _RecordingRunner()
    manager = DesktopWorkflowManager(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        runner=runner,
    )

    manager.submit("run_selection", trade_date=date(2026, 7, 31))
    manager.wait_for_idle(timeout_seconds=5)

    assert [command.module for command in runner.commands] == [
        "data_plane.cli",
        "data_plane.cli",
        "scripts.build_daily_universe",
        "scripts.build_catalyst_snapshot",
        "scripts.build_premarket_rvol",
        "scripts.build_selection_gates",
    ]
    assert manager.status()["latest_job"]["action"] == "run_selection"


def test_desktop_run_today_syncs_selects_and_starts_monitor(
    tmp_path: Path,
) -> None:
    runner = _RecordingRunner()
    manager = DesktopWorkflowManager(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        runner=runner,
        monitor=_RecordingMonitor(),
    )

    manager.submit("run_today", trade_date=date(2026, 7, 31))
    manager.wait_for_idle(timeout_seconds=5)
    deadline = time.monotonic() + 2
    while manager.status()["monitor"]["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)

    assert [command.module for command in runner.commands] == [
        "data_plane.cli",
        "data_plane.cli",
        "scripts.backfill_massive_news",
        "scripts.build_daily_universe",
        "scripts.build_catalyst_snapshot",
        "scripts.build_premarket_rvol",
        "scripts.build_selection_gates",
    ]
    assert manager.status()["latest_job"]["status"] == "complete"
    assert manager.status()["monitor"]["status"] == "complete"


def test_desktop_workflow_monitor_can_start_stop_and_persist_status(
    tmp_path: Path,
) -> None:
    manager = DesktopWorkflowManager(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        runner=_RecordingRunner(),
        monitor=_RecordingMonitor(),
    )

    started = manager.start_monitor(trade_date=date(2026, 7, 31))
    stopped = manager.stop_monitor(timeout_seconds=2)
    status = manager.status()["monitor"]

    assert started["accepted"] is True
    assert stopped["stopped"] is True
    assert status["status"] == "stopped"
    assert status["events_stored"] == 7
    assert status["symbols"] == ["AAPL"]


def test_selection_monitor_does_not_cancel_a_slow_pending_quote(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "data" / "accepted" / "kernel.universe.selection_gates-test"
    snapshot.mkdir(parents=True)
    pl.DataFrame(
        {
            "session_date": [date(2026, 7, 31)],
            "symbol": ["BRKR"],
            "pass_gate": [True],
        }
    ).write_parquet(snapshot / "data.parquet")
    stop_event = threading.Event()
    threading.Timer(1.7, stop_event.set).start()
    monitor = SelectionSipMonitor(
        environ={
            "ALPACA_PROXY_KEY": "market-key",
            "ALPACA_PROXY_SECRET": "market-secret",
        },
        stream_factory=lambda **_kwargs: _DelayedStream(),
        historical_bars_fetcher=lambda _symbols, _start, _end: pl.DataFrame(),
    )

    result = monitor.run(
        stop_event=stop_event,
        trade_date=date(2026, 7, 31),
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
    )

    assert result["events_stored"] == 1
    assert result["historical_events_stored"] == 0
    assert result["symbols"] == ["BRKR", "SPY"]
    assert SipEventStore(tmp_path / "runs" / "sip-stream.sqlite3").counts()[
        "quote_seconds"
    ] == 1


def test_desktop_workflow_streams_child_progress_before_step_completion(
    tmp_path: Path,
) -> None:
    runner = _ProgressRunner()
    manager = DesktopWorkflowManager(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        runner=runner,
    )

    manager.submit("sync_data", trade_date=date(2026, 7, 31))
    assert runner.reported.wait(1)
    active = manager.status()["active_job"]
    runner.release.set()
    manager.wait_for_idle(timeout_seconds=5)

    assert active["step_progress_current"] == 5
    assert active["step_progress_total"] == 100
    assert active["step_progress_percent"] == 5.0
    assert active["progress_detail"] == "2025-01-08"


def test_desktop_workflow_sync_starts_after_latest_bootstrap_session(
    tmp_path: Path,
) -> None:
    snapshot = (
        tmp_path
        / "data"
        / "accepted"
        / "massive.grouped_daily-bootstrap"
    )
    snapshot.mkdir(parents=True)
    pl.DataFrame({"trade_date": [date(2026, 7, 29)]}).write_parquet(
        snapshot / "data.parquet"
    )
    runner = _RecordingRunner()
    manager = DesktopWorkflowManager(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        runner=runner,
    )

    manager.submit("sync_data", trade_date=date(2026, 7, 31))
    manager.wait_for_idle(timeout_seconds=5)
    arguments = runner.commands[0].arguments

    assert arguments[arguments.index("--start") + 1] == "2026-07-30"
    assert arguments[arguments.index("--end") + 1] == "2026-07-30"
