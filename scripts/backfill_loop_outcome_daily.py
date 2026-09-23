"""Persist one validated XNYS daily snapshot from direct Alpaca SIP bars."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DatasetSnapshot
from data_plane.daily import DAILY_SCHEMA_VERSION, audit_daily_bars, canonicalize_daily_bars
from data_plane.providers.alpaca import fetch_daily_bars, stock_data_policy_from_env
from data_plane.quality import canonicalize_bars
from data_plane.storage import persist_snapshot
from operations.local_env import load_project_env, project_data_root

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "alpaca.sip.daily_event_session"
RAW_SOURCE = "alpaca.sip.rest.bars.1day"
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,15}$")


def normalize_alpaca_daily_bars(frame: pl.DataFrame, *, trade_date: date) -> pl.DataFrame:
    """Map direct Alpaca daily bars to the outcome reporter's truthful schema."""
    if frame.is_empty():
        raise ValueError(f"no Alpaca daily bars for requested XNYS session {trade_date}")
    bars = canonicalize_bars(frame)
    if set(bars.get_column("source").drop_nulls().unique().to_list()) != {RAW_SOURCE}:
        raise ValueError("daily backfill requires direct Alpaca SIP 1Day bars")
    if set(bars.get_column("feed").drop_nulls().unique().to_list()) != {"sip"}:
        raise ValueError("daily backfill requires the SIP feed")
    if set(bars.get_column("adjustment").drop_nulls().unique().to_list()) != {"split_adjusted"}:
        raise ValueError("daily backfill requires split-adjusted prices")
    daily = (
        bars.with_columns(
            pl.col("ts_utc").dt.convert_time_zone("America/New_York").dt.date().alias("trade_date"),
            pl.col("ts_utc").alias("provider_ts_utc"),
            pl.lit(SOURCE).alias("source"),
        )
        .filter(pl.col("trade_date") == trade_date)
        .select(
            "symbol",
            "trade_date",
            "provider_ts_utc",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "trade_count",
            "vwap",
            "source",
            "feed",
            "adjustment",
        )
    )
    if daily.is_empty():
        raise ValueError(f"no Alpaca daily bars for requested XNYS session {trade_date}")
    return canonicalize_daily_bars(daily)


def _persist_daily_snapshot(
    frame: pl.DataFrame,
    *,
    data_root: Path,
    trade_date: date,
) -> tuple[DatasetSnapshot, Path]:
    symbols = tuple(sorted(frame.get_column("symbol").unique().to_list()))
    symbol_hash = hashlib.sha256(",".join(symbols).encode()).hexdigest()[:16]
    provenance = f"{SOURCE}@{trade_date.isoformat()}|symbols={symbol_hash}"
    checks = audit_daily_bars(
        frame,
        provenance=provenance,
        expected_date=trade_date,
        expected_source=SOURCE,
    )
    snapshot, path = persist_snapshot(
        frame,
        root=data_root,
        source=SOURCE,
        schema_version=DAILY_SCHEMA_VERSION,
        checks=checks,
    )
    snapshot.assert_usable()
    return snapshot, path


def _parse_symbols(values: list[str]) -> tuple[str, ...]:
    symbols = tuple(sorted({item.strip().upper() for value in values for item in value.split(",")}))
    if not symbols or any(not _SYMBOL.fullmatch(item) for item in symbols):
        raise argparse.ArgumentTypeError("symbols must be comma-separated US ticker symbols")
    return symbols


def _symbols_from_outbox(path: Path) -> tuple[str, ...]:
    if not path.is_file():
        raise FileNotFoundError(f"Loop outcome outbox is missing: {path}")
    symbols: set[str] = set()
    database_uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        rows = connection.execute(
            "SELECT payload_json FROM loop_outbox "
            "WHERE event_type='outcome' AND status IN ('pending', 'failed')"
        )
        for (raw_payload,) in rows:
            payload = json.loads(raw_payload)
            instrument = payload.get("instrument")
            if instrument:
                symbols.add(str(instrument).upper())
            benchmark = (payload.get("evidence") or {}).get("benchmark_id")
            if benchmark:
                symbols.add(str(benchmark).upper())
    return _parse_symbols([*symbols])


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", type=date.fromisoformat, required=True)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--symbols", nargs="+")
    inputs.add_argument("--outbox", type=Path)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    args = parser.parse_args()
    symbols = _parse_symbols(args.symbols) if args.symbols else _symbols_from_outbox(args.outbox)
    if build_xnys_schedule(args.trade_date, args.trade_date).height != 1:
        raise ValueError(f"{args.trade_date} is not an XNYS trading session")
    policy = stock_data_policy_from_env()
    if policy.feed != "sip":
        raise RuntimeError("outcome backfill is disabled unless Alpaca SIP is selected")
    start_utc = datetime.combine(args.trade_date, time.min, UTC)
    end_utc = start_utc + timedelta(days=1)
    raw = fetch_daily_bars(symbols, start_utc, end_utc, feed="sip", adjustment="split")
    frame = normalize_alpaca_daily_bars(raw, trade_date=args.trade_date)
    snapshot, path = _persist_daily_snapshot(
        frame,
        data_root=args.data_root,
        trade_date=args.trade_date,
    )
    missing = sorted(set(symbols) - set(frame.get_column("symbol").to_list()))
    print(
        json.dumps(
            {
                "status": "accepted" if snapshot.usable else "quarantined",
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
                "trade_date": args.trade_date.isoformat(),
                "source": SOURCE,
                "symbols_requested": len(symbols),
                "rows": frame.height,
                "symbols_missing": missing,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
