"""Cross-platform weekly point-in-time research and governed evolution cycle."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from data_plane.calendar import build_xnys_schedule
from schedule.runtime import JsonEventLogger, LockUnavailableError, ProcessLock

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ResearchWindow:
    end: date
    daily_start: date
    news_start: date
    news_end_exclusive: date
    sessions: int


def research_window(end: date, *, sessions: int = 252) -> ResearchWindow:
    if sessions < 30:
        raise ValueError("research cycle requires at least 30 sessions")
    schedule = build_xnys_schedule(end - timedelta(days=sessions * 4), end)
    eligible = schedule.filter(schedule["trade_date"] <= end)["trade_date"]
    required_daily = sessions * 2
    if len(eligible) < required_daily:
        raise ValueError("calendar does not contain enough feature and target history")
    daily_dates = eligible.tail(required_daily).to_list()
    target_dates = eligible.tail(sessions).to_list()
    first_target = target_dates[0]
    news_start = first_target.replace(day=1)
    return ResearchWindow(
        end=end,
        daily_start=daily_dates[0],
        news_start=news_start,
        news_end_exclusive=end + timedelta(days=1),
        sessions=sessions,
    )


def _latest_completed_session(now_utc: datetime) -> date:
    today = now_utc.date()
    schedule = build_xnys_schedule(today - timedelta(days=10), today)
    completed = schedule.filter(schedule["market_close_utc"] + timedelta(minutes=20) <= now_utc)
    if completed.is_empty():
        raise RuntimeError("no completed XNYS session is available")
    value = completed["trade_date"][-1]
    if not isinstance(value, date):
        raise ValueError("calendar returned an invalid trade date")
    return value


def _run_stage(
    name: str,
    arguments: list[str],
    *,
    logger: JsonEventLogger,
    timeout_seconds: int,
) -> None:
    started = time.monotonic()
    logger.emit("research_stage_started", stage=name)
    completed = subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    elapsed_ms = round((time.monotonic() - started) * 1000)
    if completed.returncode != 0:
        logger.emit(
            "research_stage_failed",
            level="error",
            stage=name,
            return_code=completed.returncode,
            stdout_lines=len(completed.stdout.splitlines()),
            stderr_lines=len(completed.stderr.splitlines()),
            elapsed_ms=elapsed_ms,
        )
        raise RuntimeError(f"research stage failed: {name}")
    logger.emit("research_stage_completed", stage=name, elapsed_ms=elapsed_ms)


def _stages(
    window: ResearchWindow,
    *,
    data_root: Path,
    state_root: Path,
) -> tuple[tuple[str, list[str], int], ...]:
    common_end = window.end.isoformat()
    return (
        (
            "grouped_daily",
            [
                "-m",
                "data_plane.cli",
                "--data-root",
                str(data_root),
                "massive-grouped-daily",
                "--start",
                window.daily_start.isoformat(),
                "--end",
                common_end,
            ],
            6 * 3600,
        ),
        (
            "massive_news",
            [
                "-m",
                "scripts.backfill_massive_news",
                "--start",
                window.news_start.isoformat(),
                "--end",
                window.news_end_exclusive.isoformat(),
                "--data-root",
                str(data_root),
            ],
            6 * 3600,
        ),
        (
            "weekly_reference",
            [
                "-m",
                "scripts.backfill_massive_reference_weekly",
                "--end",
                common_end,
                "--sessions",
                str(window.sessions),
                "--data-root",
                str(data_root),
            ],
            6 * 3600,
        ),
        (
            "pit_selection",
            [
                "-m",
                "scripts.build_historical_selection",
                "--end",
                common_end,
                "--sessions",
                str(window.sessions),
                "--data-root",
                str(data_root),
            ],
            6 * 3600,
        ),
        (
            "premarket_rvol",
            [
                "-m",
                "scripts.backfill_historical_premarket",
                "--end",
                common_end,
                "--data-root",
                str(data_root),
            ],
            12 * 3600,
        ),
        (
            "selection_gates",
            [
                "-m",
                "scripts.build_historical_selection_gates",
                "--end",
                common_end,
                "--data-root",
                str(data_root),
            ],
            12 * 3600,
        ),
        (
            "net_labels_oos",
            [
                "-m",
                "scripts.backfill_historical_labels",
                "--end",
                common_end,
                "--data-root",
                str(data_root),
            ],
            12 * 3600,
        ),
        (
            "sandbox_evolution",
            ["-m", "scripts.run_rvol_sandbox", "--data-root", str(data_root)],
            3600,
        ),
        (
            "maturity_evidence",
            [
                "-m",
                "scripts.refresh_maturity_evidence",
                "--data-root",
                str(data_root),
                "--order-db",
                str(state_root / "paper-orders.sqlite3"),
                "--evidence",
                str(state_root / "maturity-evidence.json"),
            ],
            600,
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--sessions", type=int, default=252)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--state-root", type=Path, default=ROOT / "runs")
    parser.add_argument("--lock-file", type=Path, default=ROOT / "runs/research-cycle.lock")
    return parser


def run(argv: list[str] | None = None, *, now_utc: datetime | None = None) -> int:
    args = _parser().parse_args(argv)
    logger = JsonEventLogger(service="research_cycle")
    try:
        with ProcessLock(args.lock_file):
            window = research_window(
                args.end or _latest_completed_session(now_utc or datetime.now(UTC)),
                sessions=args.sessions,
            )
            logger.emit(
                "research_cycle_started",
                end=window.end.isoformat(),
                sessions=window.sessions,
                orders_submitted=0,
            )
            for name, arguments, timeout_seconds in _stages(
                window, data_root=args.data_root, state_root=args.state_root
            ):
                _run_stage(
                    name,
                    arguments,
                    logger=logger,
                    timeout_seconds=timeout_seconds,
                )
            logger.emit(
                "research_cycle_completed",
                end=window.end.isoformat(),
                orders_submitted=0,
            )
            return 0
    except LockUnavailableError:
        logger.emit("research_cycle_skipped_lock_held", level="warning")
        return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()

