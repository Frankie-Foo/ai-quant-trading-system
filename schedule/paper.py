"""Windows-friendly, DST-safe launcher for the causal local Paper session."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from data_plane.calendar import build_xnys_schedule
from execution.locked_selection import load_locked_selection
from kernel.config import load_config
from operations.feishu_base import FeishuBaseError, FeishuBaseEventClient
from operations.feishu_investment_events import record_locked_selection
from operations.local_env import load_project_env, project_data_root
from schedule.child_process import run_child
from schedule.runtime import JsonEventLogger

ROOT = Path(__file__).resolve().parents[1]


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}
NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class PaperSessionWindow:
    trade_date: date
    market_open_utc: datetime
    market_close_utc: datetime


def paper_session_window(
    now_utc: datetime,
    *,
    start_lead_minutes: int,
) -> PaperSessionWindow | None:
    """Return today's active launch window using the XNYS calendar and DST."""
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    if start_lead_minutes < 0:
        raise ValueError("start lead must not be negative")
    normalized_now = now_utc.astimezone(UTC)
    trade_date = normalized_now.astimezone(NEW_YORK).date()
    calendar = build_xnys_schedule(trade_date, trade_date)
    if calendar.height != 1:
        return None
    row = calendar.row(0, named=True)
    market_open = row["market_open_utc"]
    market_close = row["market_close_utc"]
    if not isinstance(market_open, datetime) or not isinstance(market_close, datetime):
        raise ValueError("calendar timestamps are invalid")
    if not market_open - timedelta(minutes=start_lead_minutes) <= normalized_now < market_close:
        return None
    return PaperSessionWindow(
        trade_date=trade_date,
        market_open_utc=market_open,
        market_close_utc=market_close,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    parser.add_argument("--sip-db", type=Path, default=ROOT / "runs/sip-stream.sqlite3")
    parser.add_argument(
        "--order-db",
        type=Path,
        default=ROOT / "runs/paper-orders.sqlite3",
    )
    parser.add_argument(
        "--readiness-evidence",
        type=Path,
        default=ROOT / "runs/maturity-evidence.json",
    )
    parser.add_argument(
        "--access-receipt",
        type=Path,
        default=ROOT / "runs/alpaca-access-receipt.json",
    )
    parser.add_argument(
        "--session-lock",
        type=Path,
        default=ROOT / "runs/paper-session.lock",
    )
    parser.add_argument(
        "--sip-lock",
        type=Path,
        default=ROOT / "runs/alpaca-sip.lock",
    )
    return parser


def _run_child(command: list[str], logger: JsonEventLogger) -> None:
    module = command[2] if len(command) > 2 else "unknown"
    logger.emit("child_started", module=module)
    result = run_child(
        command,
        cwd=ROOT,
        timeout_seconds=6 * 3600,
        capture_output=False,
    )
    if result.return_code != 0:
        logger.emit(
            "child_failed",
            level="error",
            module=module,
            return_code=result.return_code,
            elapsed_ms=result.elapsed_ms,
        )
        raise RuntimeError(f"{module} failed with exit code {result.return_code}")
    logger.emit("child_completed", module=module, elapsed_ms=result.elapsed_ms)


def run(argv: list[str] | None = None, *, now_utc: datetime | None = None) -> int:
    load_project_env(ROOT)
    args = _parser().parse_args(argv)
    logger = JsonEventLogger(service="paper_scheduler")
    cfg = load_config(ROOT / "config.yaml")
    window = paper_session_window(
        now_utc or datetime.now(UTC),
        start_lead_minutes=cfg.market_data.paper_start_lead_minutes,
    )
    if window is None:
        logger.emit("paper_launch_not_due")
        return 0
    try:
        selection = load_locked_selection(
            args.data_root,
            window.trade_date,
            min_rvol=cfg.universe.min_rvol,
        )
    except (FileNotFoundError, ValueError) as exc:
        logger.emit(
            "paper_launch_waiting_selection",
            level="warning",
            trade_date=window.trade_date.isoformat(),
            error_type=type(exc).__name__,
        )
        return 0
    try:
        feishu = FeishuBaseEventClient.from_environment(os.environ)
    except RuntimeError as exc:
        logger.emit(
            "feishu_selection_configuration_invalid",
            level="critical",
            error_type=type(exc).__name__,
            trade_date=window.trade_date.isoformat(),
        )
        return 1
    try:
        if feishu is not None:
            record_locked_selection(
                feishu,
                selection,
                observed_at_utc=(now_utc or datetime.now(UTC)),
            )
    except (FeishuBaseError, RuntimeError, ValueError) as exc:
        logger.emit(
            "feishu_selection_write_failed",
            level="error",
            error_type=type(exc).__name__,
            trade_date=window.trade_date.isoformat(),
        )
        if _truthy(os.environ.get("FEISHU_INVESTMENT_AUDIT_REQUIRED")):
            return 1

    common_evidence = [
        "--data-root",
        str(args.data_root),
        "--order-db",
        str(args.order_db),
        "--evidence",
        str(args.readiness_evidence),
    ]
    _run_child(
        [
            sys.executable,
            "-m",
            "scripts.verify_alpaca_access",
            "--symbols",
            selection.symbols[0],
            "--lock-file",
            str(args.sip_lock),
            "--receipt",
            str(args.access_receipt),
            *common_evidence,
        ],
        logger,
    )
    _run_child(
        [
            sys.executable,
            "-m",
            "scripts.refresh_maturity_evidence",
            *common_evidence,
        ],
        logger,
    )
    _run_child(
        [
            sys.executable,
            "-m",
            "scripts.run_paper_session",
            "--trade-date",
            window.trade_date.isoformat(),
            "--data-root",
            str(args.data_root),
            "--sip-db",
            str(args.sip_db),
            "--order-db",
            str(args.order_db),
            "--readiness-evidence",
            str(args.readiness_evidence),
            "--lock-file",
            str(args.session_lock),
            "--sip-lock-file",
            str(args.sip_lock),
        ],
        logger,
    )
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
