from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import polars as pl

from data_plane.http import DownloadError, get_json
from data_plane.quality import canonicalize_bars

BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"


def fetch_recent_bars(symbols: tuple[str, ...], *, range_: str = "7d") -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for symbol in symbols:
        payload = get_json(
            f"{BASE_URL}/{symbol}",
            params={
                "interval": "1m",
                "range": range_,
                "includePrePost": "true",
                "events": "div,splits",
            },
            headers={"User-Agent": "Mozilla/5.0 trading-system-v2-research"},
        )
        chart = payload.get("chart")
        if not isinstance(chart, dict) or chart.get("error"):
            raise DownloadError(f"Yahoo chart error for {symbol}: {chart}")
        results = chart.get("result")
        if not isinstance(results, list) or not results:
            raise DownloadError(f"Yahoo returned no chart result for {symbol}")
        result = results[0]
        if not isinstance(result, dict):
            raise DownloadError(f"Yahoo returned malformed result for {symbol}")
        timestamps = result.get("timestamp", [])
        indicators = result.get("indicators", {})
        if not isinstance(timestamps, list) or not isinstance(indicators, dict):
            raise DownloadError(f"Yahoo returned malformed timestamps for {symbol}")
        quotes = indicators.get("quote", [])
        if not isinstance(quotes, list) or not quotes or not isinstance(quotes[0], dict):
            raise DownloadError(f"Yahoo returned no quotes for {symbol}")
        quote: dict[str, Any] = quotes[0]
        for index, timestamp in enumerate(timestamps):
            row = _row_at(symbol, int(timestamp), quote, index)
            if row is not None:
                rows.append(row)
    frame = pl.DataFrame(rows) if rows else _empty_frame()
    return canonicalize_bars(frame)


def _row_at(
    symbol: str,
    timestamp: int,
    quote: dict[str, Any],
    index: int,
) -> dict[str, object] | None:
    def value(column: str) -> object:
        values = quote.get(column, [])
        return values[index] if isinstance(values, list) and index < len(values) else None

    prices = {name: value(name) for name in ("open", "high", "low", "close")}
    if any(prices[name] is None for name in prices):
        return None
    return {
        "symbol": symbol,
        "ts_utc": datetime.fromtimestamp(timestamp, UTC),
        **prices,
        "volume": value("volume"),
        "trade_count": None,
        "vwap": None,
        "source": "yahoo.chart",
        "feed": "undocumented",
        "adjustment": "raw_unverified",
    }


def _empty_frame() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "symbol": pl.String,
            "ts_utc": pl.Datetime("ms", "UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
            "trade_count": pl.Int64,
            "vwap": pl.Float64,
            "source": pl.String,
            "feed": pl.String,
            "adjustment": pl.String,
        }
    )
