"""Backfill complete SPY SIP RTH minute bars for Noise-Area research."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, QualitySeverity
from data_plane.providers.alpaca import fetch_bars
from data_plane.storage import persist_snapshot
from operations.local_env import load_project_env

ROOT = Path(__file__).resolve().parents[1]


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _check(name: str, passed: bool, observed: object, expected: str) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=QualitySeverity.CRITICAL,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance="scripts.backfill_spy_noise_area_data.v1",
    )


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=_parse_date, default=date(2024, 1, 1))
    parser.add_argument("--end", type=_parse_date, default=date.today())
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--premarket-minutes", type=int, default=0)
    parser.add_argument("--input", type=Path)
    parser.add_argument(
        "--data-root", type=Path, default=ROOT / "runtime" / "ai-quant" / "data"
    )
    args = parser.parse_args()
    if args.end <= args.start:
        raise ValueError("end must be after start")
    symbol = args.symbol.strip().upper()
    if not symbol.isalnum():
        raise ValueError("symbol must be alphanumeric")
    if not 0 <= args.premarket_minutes <= 60:
        raise ValueError("premarket-minutes must be between 0 and 60")
    prefix = f"{args.premarket_minutes}m_pre_" if args.premarket_minutes else ""
    source = f"research.alpaca_sip.{symbol.lower()}_{prefix}rth_1m"
    start_utc = datetime.combine(args.start, time.min, tzinfo=UTC)
    end_utc = datetime.combine(args.end, time.min, tzinfo=UTC)
    frame = (
        pl.read_parquet(args.input)
        if args.input is not None
        else fetch_bars((symbol,), start_utc, end_utc, feed="sip")
    )
    schedule = (
        build_xnys_schedule(args.start, args.end)
        .filter(pl.col("trade_date") < args.end)
        .select("trade_date", "market_open_utc", "market_close_utc")
    )
    frame = (
        frame.with_columns(
            pl.col("ts_utc")
            .dt.convert_time_zone("America/New_York")
            .dt.date()
            .alias("trade_date")
        )
        .join(schedule, on="trade_date", how="inner")
        .filter(
            (
                pl.col("ts_utc")
                >= pl.col("market_open_utc")
                - timedelta(minutes=args.premarket_minutes)
            )
            & (pl.col("ts_utc") < pl.col("market_close_utc"))
        )
        .drop("trade_date", "market_open_utc", "market_close_utc")
        .sort("ts_utc")
    )
    expected_rows = sum(
        int(
            (row["market_close_utc"] - row["market_open_utc"]).total_seconds() / 60
            + args.premarket_minutes
        )
        for row in schedule.iter_rows(named=True)
    )
    minimum_rows = expected_rows - max(
        0, int(schedule.height * args.premarket_minutes * 0.001)
    )
    unique_rows = frame.select(pl.struct("symbol", "ts_utc").n_unique()).item()
    snapshot, path = persist_snapshot(
        frame,
        root=args.data_root,
        source=source,
        schema_version="single_symbol_rth_1m.v1",
        checks=(
            _check("non_empty", frame.height > 0, frame.height, ">0"),
            _check(
                "only_requested_symbol",
                frame.get_column("symbol").unique().to_list() == [symbol],
                symbol,
                symbol,
            ),
            _check(
                "unique_symbol_timestamp",
                unique_rows == frame.height,
                unique_rows,
                str(frame.height),
            ),
            _check(
                "calendar_minute_coverage",
                minimum_rows <= frame.height <= expected_rows,
                frame.height,
                f"{minimum_rows}..{expected_rows}",
            ),
        ),
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {"dataset_id": snapshot.dataset_id, "rows": frame.height, "path": str(path)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
