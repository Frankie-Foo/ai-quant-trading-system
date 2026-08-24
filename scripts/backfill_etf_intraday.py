"""Backfill accepted split-adjusted SIP RTH minute bars for liquid ETFs."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.providers.alpaca import fetch_bars, stock_data_policy_from_env
from data_plane.quality import BAR_SCHEMA_VERSION, audit_minute_bars
from data_plane.storage import persist_snapshot
from operations.local_env import load_project_env, project_data_root

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "alpaca.sip.etf_rth_1m"
SYMBOLS = ("SPY", "QQQ", "IWM")


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _rth_only(frame: pl.DataFrame, schedule: pl.DataFrame) -> pl.DataFrame:
    return (
        frame.with_columns(
            pl.col("ts_utc")
            .dt.convert_time_zone("America/New_York")
            .dt.date()
            .alias("trade_date")
        )
        .join(schedule, on="trade_date", how="inner")
        .filter(
            (pl.col("ts_utc") >= pl.col("market_open_utc"))
            & (pl.col("ts_utc") < pl.col("market_close_utc"))
        )
        .drop("trade_date", "market_open_utc", "market_close_utc")
        .sort("symbol", "ts_utc")
    )


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=_parse_date, required=True)
    parser.add_argument("--end", type=_parse_date, required=True)
    parser.add_argument("--chunk-sessions", type=int, default=126)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    args = parser.parse_args()
    if args.end < args.start or args.chunk_sessions <= 0:
        raise ValueError("invalid date range or chunk size")
    schedule = build_xnys_schedule(args.start, args.end).select(
        "trade_date", "market_open_utc", "market_close_utc"
    )
    policy = stock_data_policy_from_env()
    for offset in range(0, schedule.height, args.chunk_sessions):
        chunk = schedule.slice(offset, args.chunk_sessions)
        start_utc = chunk["market_open_utc"][0]
        end_utc = chunk["market_close_utc"][-1]
        frame = _rth_only(
            fetch_bars(SYMBOLS, start_utc, end_utc, feed=policy.feed), chunk
        )
        checks = audit_minute_bars(
            frame,
            provenance=f"{SOURCE}@{start_utc.isoformat()}..{end_utc.isoformat()}",
            expected_symbols=SYMBOLS,
            research_approved=policy.feed == "sip" and policy.is_realtime,
        )
        snapshot, path = persist_snapshot(
            frame,
            root=args.data_root,
            source=SOURCE,
            schema_version=BAR_SCHEMA_VERSION,
            checks=checks,
        )
        print(
            json.dumps(
                {
                    "dataset_id": snapshot.dataset_id,
                    "sessions": chunk.height,
                    "rows": frame.height,
                    "usable": snapshot.usable,
                    "path": str(path),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        snapshot.assert_usable()


if __name__ == "__main__":
    main()
