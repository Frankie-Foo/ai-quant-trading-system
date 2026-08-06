from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
from dotenv import load_dotenv

from data_plane.calendar import CALENDAR_SCHEMA_VERSION, build_xnys_schedule
from data_plane.contracts import DataQualityCheck, QualitySeverity
from data_plane.daily import DAILY_SCHEMA_VERSION, audit_daily_bars
from data_plane.providers import alpaca, huggingface, massive, reference, yahoo
from data_plane.quality import BAR_SCHEMA_VERSION, audit_minute_bars
from data_plane.storage import persist_snapshot

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = ROOT / "data"


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone, preferably Z")
    return parsed.astimezone(UTC)


def parse_symbols(value: str) -> tuple[str, ...]:
    symbols = tuple(sorted({part.strip().upper() for part in value.split(",") if part.strip()}))
    if not symbols:
        raise argparse.ArgumentTypeError("at least one symbol is required")
    return symbols


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _store_bars(
    frame: pl.DataFrame,
    *,
    source: str,
    symbols: tuple[str, ...],
    research_approved: bool,
    data_root: Path,
) -> dict[str, Any]:
    checks = audit_minute_bars(
        frame,
        provenance=f"{source}.download@{datetime.now(UTC).isoformat()}",
        expected_symbols=symbols,
        research_approved=research_approved,
    )
    snapshot, path = persist_snapshot(
        frame,
        root=data_root,
        source=source,
        schema_version=BAR_SCHEMA_VERSION,
        checks=checks,
    )
    return {
        "dataset_id": snapshot.dataset_id,
        "usable": snapshot.usable,
        "rows": snapshot.row_count,
        "path": str(path),
        "failed_checks": [check.name for check in checks if not check.passed],
    }


def _reference_checks(frame: pl.DataFrame, source: str) -> tuple[DataQualityCheck, ...]:
    duplicate_count = frame.select(pl.col("symbol").is_duplicated().sum()).item()
    return (
        DataQualityCheck(
            name="non_empty",
            severity=QualitySeverity.CRITICAL,
            passed=frame.height > 0,
            observed=str(frame.height),
            expected="row_count > 0",
            provenance=source,
        ),
        DataQualityCheck(
            name="unique_symbol",
            severity=QualitySeverity.CRITICAL,
            passed=duplicate_count == 0,
            observed=str(duplicate_count),
            expected="0 duplicate symbols",
            provenance=source,
        ),
        DataQualityCheck(
            name="point_in_time_scope",
            severity=QualitySeverity.WARNING,
            passed=False,
            observed="current directory only",
            expected="do not use as a historical universe",
            provenance=source,
        ),
    )


def _calendar_checks(frame: pl.DataFrame, source: str) -> tuple[DataQualityCheck, ...]:
    duplicate_count = frame.select(pl.col("trade_date").is_duplicated().sum()).item()
    invalid_sessions = frame.filter(
        (pl.col("market_close_utc") <= pl.col("market_open_utc"))
        | ~pl.col("session_minutes").is_in([210, 390])
    ).height
    timezone_columns = (
        str(frame.schema.get("market_open_utc")),
        str(frame.schema.get("market_close_utc")),
    )
    expected_timezone = "Datetime(time_unit='ms', time_zone='UTC')"
    return (
        DataQualityCheck(
            name="non_empty",
            severity=QualitySeverity.CRITICAL,
            passed=frame.height > 0,
            observed=str(frame.height),
            expected="row_count > 0",
            provenance=source,
        ),
        DataQualityCheck(
            name="unique_trade_date",
            severity=QualitySeverity.CRITICAL,
            passed=duplicate_count == 0,
            observed=str(duplicate_count),
            expected="0 duplicate trading dates",
            provenance=source,
        ),
        DataQualityCheck(
            name="valid_session_duration",
            severity=QualitySeverity.CRITICAL,
            passed=invalid_sessions == 0,
            observed=str(invalid_sessions),
            expected="NYSE sessions are 390 minutes or 210-minute early closes",
            provenance=source,
        ),
        DataQualityCheck(
            name="utc_timestamps",
            severity=QualitySeverity.CRITICAL,
            passed=all(item == expected_timezone for item in timezone_columns),
            observed=str(timezone_columns),
            expected="open and close are Datetime(ms, UTC)",
            provenance=source,
        ),
    )


def _massive_reference_checks(
    frame: pl.DataFrame,
    *,
    asof_date: date,
    active: bool,
    security_type: str | None,
) -> tuple[DataQualityCheck, ...]:
    duplicate_count = frame.select(pl.col("symbol").is_duplicated().sum()).item()
    wrong_dates = frame.filter(pl.col("asof_date") != asof_date).height
    wrong_status = frame.filter(pl.col("active") != active).height
    wrong_type = (
        frame.filter(pl.col("security_type") != security_type).height if security_type else 0
    )
    provenance = f"massive.reference_tickers@{asof_date.isoformat()}"
    return (
        DataQualityCheck(
            name="non_empty",
            severity=QualitySeverity.CRITICAL,
            passed=frame.height > 0,
            observed=str(frame.height),
            expected="row_count > 0",
            provenance=provenance,
        ),
        DataQualityCheck(
            name="unique_symbol",
            severity=QualitySeverity.CRITICAL,
            passed=duplicate_count == 0,
            observed=str(duplicate_count),
            expected="0 duplicate symbols",
            provenance=provenance,
        ),
        DataQualityCheck(
            name="point_in_time_date",
            severity=QualitySeverity.CRITICAL,
            passed=wrong_dates == 0,
            observed=str(wrong_dates),
            expected=asof_date.isoformat(),
            provenance=provenance,
        ),
        DataQualityCheck(
            name="requested_active_status",
            severity=QualitySeverity.CRITICAL,
            passed=wrong_status == 0,
            observed=str(wrong_status),
            expected=str(active),
            provenance=provenance,
        ),
        DataQualityCheck(
            name="requested_security_type",
            severity=QualitySeverity.CRITICAL,
            passed=wrong_type == 0,
            observed=str(wrong_type),
            expected=security_type or "all security types",
            provenance=provenance,
        ),
    )


def _existing_massive_daily_dates(data_root: Path) -> set[date]:
    dates: set[date] = set()
    for path in (data_root / "accepted").glob("massive.grouped_daily-*/data.parquet"):
        values = pl.read_parquet(path, columns=["trade_date"]).get_column("trade_date").unique()
        dates.update(values.to_list())
    return dates


def _download_massive_daily_range(
    *,
    start_date: date,
    end_date: date,
    data_root: Path,
    pace_seconds: float,
) -> dict[str, Any]:
    calendar = build_xnys_schedule(start_date, end_date)
    requested_dates: list[date] = calendar.get_column("trade_date").to_list()
    existing = _existing_massive_daily_dates(data_root)
    pending = [item for item in requested_dates if item not in existing]
    downloaded: list[str] = []
    rows = 0
    for index, trade_date in enumerate(pending, start=1):
        request_started = time.monotonic()
        frame = massive.fetch_grouped_daily(trade_date)
        checks = audit_daily_bars(
            frame,
            provenance=f"massive.grouped_daily@{trade_date.isoformat()}",
            expected_date=trade_date,
        )
        snapshot, path = persist_snapshot(
            frame,
            root=data_root,
            source="massive.grouped_daily",
            schema_version=DAILY_SCHEMA_VERSION,
            checks=checks,
        )
        if not snapshot.usable:
            raise RuntimeError(f"grouped daily snapshot quarantined at {path}")
        downloaded.append(snapshot.dataset_id)
        rows += snapshot.row_count
        print(
            json.dumps(
                {
                    "progress": f"{index}/{len(pending)}",
                    "trade_date": trade_date.isoformat(),
                    "rows": snapshot.row_count,
                    "dataset_id": snapshot.dataset_id,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if index < len(pending):
            elapsed = time.monotonic() - request_started
            if elapsed < pace_seconds:
                time.sleep(pace_seconds - elapsed)
    return {
        "requested_sessions": len(requested_dates),
        "already_present": len(requested_dates) - len(pending),
        "downloaded_sessions": len(downloaded),
        "downloaded_rows": rows,
        "dataset_ids": downloaded,
    }
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Auditable market-data ingestion")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    bars_common = argparse.ArgumentParser(add_help=False)
    bars_common.add_argument("--symbols", type=parse_symbols, required=True)
    bars_common.add_argument("--start", type=parse_utc, required=True)
    bars_common.add_argument("--end", type=parse_utc, required=True)

    subparsers.add_parser("hf-staging", parents=[bars_common])
    subparsers.add_parser("alpaca", parents=[bars_common])
    subparsers.add_parser("massive", parents=[bars_common])

    grouped_parser = subparsers.add_parser("massive-grouped-daily")
    grouped_parser.add_argument("--start", type=parse_date, required=True)
    grouped_parser.add_argument("--end", type=parse_date, required=True)
    grouped_parser.add_argument("--pace-seconds", type=float, default=12.5)

    massive_reference_parser = subparsers.add_parser("massive-reference")
    massive_reference_parser.add_argument("--date", type=parse_date, required=True)
    massive_reference_parser.add_argument("--inactive", action="store_true")
    massive_reference_parser.add_argument("--all-types", action="store_true")
    massive_reference_parser.add_argument("--pace-seconds", type=float, default=12.5)

    yahoo_parser = subparsers.add_parser("yahoo-staging")
    yahoo_parser.add_argument("--symbols", type=parse_symbols, required=True)
    yahoo_parser.add_argument("--range", default="7d", choices=("1d", "5d", "7d", "1mo"))

    subparsers.add_parser("nasdaq-reference")
    subparsers.add_parser("sec-reference")
    calendar_parser = subparsers.add_parser("calendar")
    calendar_parser.add_argument("--start", type=parse_date, required=True)
    calendar_parser.add_argument("--end", type=parse_date, required=True)
    subparsers.add_parser("credentials")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    args = build_parser().parse_args(argv)
    data_root: Path = args.data_root

    if args.command == "hf-staging":
        result = _store_bars(
            huggingface.fetch_staging_bars(args.symbols, args.start, args.end),
            source="huggingface.crypto_spartan",
            symbols=args.symbols,
            research_approved=False,
            data_root=data_root,
        )
    elif args.command == "yahoo-staging":
        result = _store_bars(
            yahoo.fetch_recent_bars(args.symbols, range_=args.range),
            source="yahoo.chart",
            symbols=args.symbols,
            research_approved=False,
            data_root=data_root,
        )
    elif args.command == "alpaca":
        result = _store_bars(
            alpaca.fetch_bars(args.symbols, args.start, args.end),
            source="alpaca.sip.adjusted",
            symbols=args.symbols,
            research_approved=True,
            data_root=data_root,
        )
    elif args.command == "massive":
        result = _store_bars(
            massive.fetch_bars(args.symbols, args.start, args.end),
            source="massive.sip.adjusted",
            symbols=args.symbols,
            research_approved=True,
            data_root=data_root,
        )
    elif args.command == "massive-grouped-daily":
        if args.pace_seconds < 0:
            raise ValueError("pace-seconds must be nonnegative")
        result = _download_massive_daily_range(
            start_date=args.start,
            end_date=args.end,
            data_root=data_root,
            pace_seconds=args.pace_seconds,
        )
    elif args.command == "massive-reference":
        active = not args.inactive
        security_type = None if args.all_types else "CS"

        def report_page(page: int, rows: int) -> None:
            print(
                json.dumps({"page": page, "cumulative_rows": rows}),
                flush=True,
            )

        frame = massive.fetch_ticker_reference(
            args.date,
            active=active,
            security_type=security_type,
            pace_seconds=args.pace_seconds,
            on_page=report_page,
        )
        checks = _massive_reference_checks(
            frame,
            asof_date=args.date,
            active=active,
            security_type=security_type,
        )
        snapshot, path = persist_snapshot(
            frame,
            root=data_root,
            source="massive.reference_tickers.cs" if security_type else "massive.reference_tickers",
            schema_version="ticker_reference_pit.v1",
            checks=checks,
        )
        result = {
            "dataset_id": snapshot.dataset_id,
            "usable": snapshot.usable,
            "rows": snapshot.row_count,
            "path": str(path),
            "failed_checks": [check.name for check in checks if not check.passed],
        }
    elif args.command == "nasdaq-reference":
        frame = reference.fetch_nasdaq_symbol_directory()
        checks = _reference_checks(frame, "nasdaq_trader.symbol_directory")
        snapshot, path = persist_snapshot(
            frame,
            root=data_root,
            source="nasdaq_trader.current_symbols",
            schema_version="symbol_master_current.v1",
            checks=checks,
        )
        result = {
            "dataset_id": snapshot.dataset_id,
            "usable": snapshot.usable,
            "rows": snapshot.row_count,
            "path": str(path),
            "failed_checks": [check.name for check in checks if not check.passed],
        }
    elif args.command == "sec-reference":
        frame = reference.fetch_sec_company_tickers()
        checks = _reference_checks(frame, "sec.company_tickers")
        snapshot, path = persist_snapshot(
            frame,
            root=data_root,
            source="sec.current_company_tickers",
            schema_version="sec_company_tickers_current.v1",
            checks=checks,
        )
        result = {
            "dataset_id": snapshot.dataset_id,
            "usable": snapshot.usable,
            "rows": snapshot.row_count,
            "path": str(path),
            "failed_checks": [check.name for check in checks if not check.passed],
        }
    elif args.command == "calendar":
        frame = build_xnys_schedule(args.start, args.end)
        checks = _calendar_checks(frame, "pandas_market_calendars.NYSE")
        snapshot, path = persist_snapshot(
            frame,
            root=data_root,
            source="exchange_calendar.xnys",
            schema_version=CALENDAR_SCHEMA_VERSION,
            checks=checks,
        )
        result = {
            "dataset_id": snapshot.dataset_id,
            "usable": snapshot.usable,
            "rows": snapshot.row_count,
            "path": str(path),
            "failed_checks": [check.name for check in checks if not check.passed],
        }
    elif args.command == "credentials":
        result = {
            "cloud_market_data": bool(
                os.getenv("CLOUD_PLATFORM_BASE_URL")
                and os.getenv("CLOUD_MARKET_DATA_API_TOKEN")
            ),
            "massive": bool(os.getenv("MASSIVE_API_KEY")),
            "sec_user_agent": bool(os.getenv("SEC_USER_AGENT")),
        }
    else:
        raise AssertionError(f"unhandled command {args.command}")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
