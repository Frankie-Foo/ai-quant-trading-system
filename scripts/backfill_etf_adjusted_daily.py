"""Backfill adjusted daily prices for the nine-ETF momentum research universe."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import polars as pl

from data_plane.contracts import DataQualityCheck, QualitySeverity
from data_plane.storage import persist_snapshot
from operations.local_env import project_data_root

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "yahoo.finance.etf_adjusted_daily"
SYMBOLS = ("SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "GLD", "VNQ", "DBC")


def _parse_chart(symbol: str, payload: dict[str, Any]) -> list[dict[str, object]]:
    chart = payload.get("chart", {})
    if chart.get("error") is not None or not chart.get("result"):
        raise RuntimeError(f"Yahoo chart request failed for {symbol}: {chart.get('error')}")
    result = chart["result"][0]
    timestamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    adjusted = result["indicators"]["adjclose"][0]["adjclose"]
    rows: list[dict[str, object]] = []
    for stamp, close, adj_close, volume in zip(
        timestamps, quote["close"], adjusted, quote["volume"], strict=True
    ):
        if close is None or adj_close is None or volume is None:
            continue
        rows.append(
            {
                "symbol": symbol,
                "ts_utc": datetime.fromtimestamp(int(stamp), UTC),
                "close": float(close),
                "adjusted_close": float(adj_close),
                "volume": int(volume),
                "adjustment": "split_dividend_adjusted",
            }
        )
    return rows


def _fetch(symbol: str, start: date, end: date) -> list[dict[str, object]]:
    params = urlencode(
        {
            "period1": int(datetime.combine(start, datetime.min.time(), UTC).timestamp()),
            "period2": int(
                datetime.combine(end + timedelta(days=1), datetime.min.time(), UTC).timestamp()
            ),
            "interval": "1d",
            "events": "div,splits",
            "includeAdjustedClose": "true",
        }
    )
    request = Request(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{params}",
        headers={"User-Agent": "Mozilla/5.0 AI-Quant research"},
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS host
        return _parse_chart(symbol, json.load(response))


def _checks(frame: pl.DataFrame, start: date, end: date) -> tuple[DataQualityCheck, ...]:
    provenance = f"{SOURCE}@{start.isoformat()}..{end.isoformat()}"
    dates = frame.with_columns(pl.col("ts_utc").dt.date().alias("date"))
    duplicates = frame.height - frame.select(pl.struct("symbol", "ts_utc").n_unique()).item()
    counts = dates.group_by("symbol").agg(
        pl.col("date").min().alias("first"),
        pl.col("date").max().alias("last"),
        pl.len().alias("rows"),
    )
    symbols = set(counts["symbol"].to_list())
    earliest_ok = all(value <= start + timedelta(days=10) for value in counts["first"])
    latest_ok = all(value >= end - timedelta(days=10) for value in counts["last"])
    union_dates = dates["date"].n_unique()
    common_dates = (
        dates.group_by("date")
        .agg(pl.col("symbol").n_unique().alias("symbols"))
        .filter(pl.col("symbols") == len(SYMBOLS))
        .height
    )
    return (
        DataQualityCheck(
            name="expected_symbols",
            severity=QualitySeverity.CRITICAL,
            passed=symbols == set(SYMBOLS),
            observed=",".join(sorted(symbols)),
            expected=",".join(sorted(SYMBOLS)),
            provenance=provenance,
        ),
        DataQualityCheck(
            name="unique_symbol_timestamp",
            severity=QualitySeverity.CRITICAL,
            passed=duplicates == 0,
            observed=str(duplicates),
            expected="0",
            provenance=provenance,
        ),
        DataQualityCheck(
            name="positive_adjusted_prices",
            severity=QualitySeverity.CRITICAL,
            passed=frame.filter(pl.col("adjusted_close") <= 0).is_empty(),
            observed=str(frame.filter(pl.col("adjusted_close") <= 0).height),
            expected="0",
            provenance=provenance,
        ),
        DataQualityCheck(
            name="requested_date_coverage",
            severity=QualitySeverity.CRITICAL,
            passed=earliest_ok and latest_ok,
            observed=f"earliest={earliest_ok},latest={latest_ok}",
            expected="all symbols cover requested window within 10 calendar days",
            provenance=provenance,
        ),
        DataQualityCheck(
            name="common_session_ratio",
            severity=QualitySeverity.CRITICAL,
            passed=union_dates > 0 and common_dates / union_dates >= 0.95,
            observed=f"{common_dates}/{union_dates}",
            expected=">=0.95",
            provenance=provenance,
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2007, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    args = parser.parse_args()
    if args.end < args.start:
        raise ValueError("end must not precede start")
    rows = [row for symbol in SYMBOLS for row in _fetch(symbol, args.start, args.end)]
    frame = pl.DataFrame(rows, infer_schema_length=None).sort("symbol", "ts_utc")
    snapshot, path = persist_snapshot(
        frame,
        root=args.data_root,
        source=SOURCE,
        schema_version="etf_adjusted_daily.v1",
        checks=_checks(frame, args.start, args.end),
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "dataset_id": snapshot.dataset_id,
                "rows": frame.height,
                "path": str(path),
                "usable": snapshot.usable,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
