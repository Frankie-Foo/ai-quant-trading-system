from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx
import polars as pl

from data_plane.http import DownloadError
from data_plane.quality import canonicalize_bars, nullable_float

PLATFORM_API_VERSION = "v1"
AlpacaStockFeed = Literal["sip", "delayed_sip"]


@dataclass(frozen=True)
class AlpacaStockDataPolicy:
    """Licensed SIP freshness policy shared by every REST market-data consumer."""

    feed: AlpacaStockFeed
    delay_minutes: int
    is_realtime: bool


def stock_data_policy_from_env() -> AlpacaStockDataPolicy:
    """Resolve Alpaca feed semantics without silently inventing a delay."""

    raw = os.getenv("CLOUD_MARKET_DATA_FEED", "sip").strip().lower()
    if raw == "sip":
        return AlpacaStockDataPolicy(feed="sip", delay_minutes=0, is_realtime=True)
    if raw == "delayed_sip":
        return AlpacaStockDataPolicy(
            feed="delayed_sip",
            delay_minutes=15,
            is_realtime=False,
        )
    raise RuntimeError(
        "CLOUD_MARKET_DATA_FEED must be 'sip' or 'delayed_sip'; "
        "IEX is not accepted for full-market decisions"
    )


def platform_access_from_env() -> tuple[str, str]:
    base_url = os.getenv("CLOUD_PLATFORM_BASE_URL", "").strip().rstrip("/")
    token = os.getenv("CLOUD_MARKET_DATA_API_TOKEN", "").strip()
    if not base_url or not token:
        raise RuntimeError(
            "missing CLOUD_PLATFORM_BASE_URL/CLOUD_MARKET_DATA_API_TOKEN"
        )
    if not base_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise RuntimeError("cloud platform API must use HTTPS outside localhost")
    return base_url, token


def _remote_payload(
    endpoint: str,
    *,
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
    client: httpx.Client | None,
) -> tuple[list[dict[str, object]], dict[str, Any]]:
    base_url, token = platform_access_from_env()
    owns_client = client is None
    loopback = base_url.startswith(("http://127.0.0.1", "http://localhost"))
    http_client = client or httpx.Client(timeout=60, trust_env=not loopback)
    try:
        response = http_client.get(
            f"{base_url}/{PLATFORM_API_VERSION}/market-data/{endpoint}",
            params={
                "symbols": ",".join(symbols),
                "start": start_utc.isoformat(),
                "end": end_utc.isoformat(),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise DownloadError("cloud market-data API request failed") from exc
    finally:
        if owns_client:
            http_client.close()
    if not isinstance(payload, dict) or payload.get("api_version") != PLATFORM_API_VERSION:
        raise DownloadError("cloud market-data API contract is invalid")
    rows = payload.get(endpoint)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise DownloadError("cloud market-data API rows are invalid")
    coverage = payload.get("coverage")
    if not isinstance(coverage, dict):
        raise DownloadError("cloud market-data coverage contract is invalid")
    coverage_status = coverage.get("status")
    fallback_recommended = coverage.get("fallback_recommended")
    symbol_coverage = coverage.get("symbols")
    if (
        not isinstance(coverage_status, str)
        or not isinstance(fallback_recommended, bool)
        or not isinstance(symbol_coverage, list)
        or any(not isinstance(item, dict) for item in symbol_coverage)
    ):
        raise DownloadError("cloud market-data coverage contract is invalid")
    returned_symbols = [
        str(item.get("symbol", "")).strip().upper() for item in symbol_coverage
    ]
    requested_symbols = [symbol.strip().upper() for symbol in symbols]
    if (
        len(returned_symbols) != len(requested_symbols)
        or set(returned_symbols) != set(requested_symbols)
    ):
        raise DownloadError("cloud market-data coverage contract is invalid")
    return rows, coverage


def _remote_rows(
    endpoint: str,
    *,
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
    client: httpx.Client | None,
) -> list[dict[str, object]]:
    rows, coverage = _remote_payload(
        endpoint,
        symbols=symbols,
        start_utc=start_utc,
        end_utc=end_utc,
        client=client,
    )
    if coverage["fallback_recommended"] is True:
        raise DownloadError("cloud market-data coverage is not usable")
    return rows


def fetch_bars(
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
    *,
    feed: AlpacaStockFeed | None = None,
    client: httpx.Client | None = None,
) -> pl.DataFrame:
    selected_feed = feed or stock_data_policy_from_env().feed
    if not symbols:
        raise ValueError("at least one symbol is required")
    rows = _remote_rows(
        "bars",
        symbols=symbols,
        start_utc=start_utc,
        end_utc=end_utc,
        client=client,
    )
    for row in rows:
        row["vwap"] = nullable_float(row.get("vwap"))
        row["source"] = "cloud.alpaca.market_data"
        row["feed"] = selected_feed
        row["adjustment"] = "split_adjusted"

    frame = pl.DataFrame(rows) if rows else _empty_frame()
    return canonicalize_bars(frame).filter(
        (pl.col("ts_utc") >= start_utc) & (pl.col("ts_utc") < end_utc)
    )


def fetch_sparse_bars_for_monitoring(
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
    *,
    feed: AlpacaStockFeed | None = None,
    client: httpx.Client | None = None,
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Read observed minute bars for advisory charts while preserving gap evidence.

    This function intentionally has a monitoring-specific name.  It must not be
    used by execution or training paths because it permits upstream coverage to
    report sparse/no-trade minutes.
    """

    selected_feed = feed or stock_data_policy_from_env().feed
    if not symbols:
        raise ValueError("at least one symbol is required")
    rows, coverage = _remote_payload(
        "bars",
        symbols=symbols,
        start_utc=start_utc,
        end_utc=end_utc,
        client=client,
    )
    if not rows:
        raise DownloadError("cloud market-data API returned no observed bars")
    status = coverage.get("status")
    if status not in {"observed", "gaps_detected"}:
        raise DownloadError("cloud market-data coverage is not observable")
    for row in rows:
        row["vwap"] = nullable_float(row.get("vwap"))
        row["source"] = "cloud.alpaca.market_data"
        row["feed"] = selected_feed
        row["adjustment"] = "split_adjusted"
    frame = canonicalize_bars(pl.DataFrame(rows)).filter(
        (pl.col("ts_utc") >= start_utc) & (pl.col("ts_utc") < end_utc)
    )
    return frame, coverage


def fetch_quotes(
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
    *,
    feed: AlpacaStockFeed | None = None,
    client: httpx.Client | None = None,
) -> pl.DataFrame:
    """Fetch historical SIP NBBO quote updates without reducing timestamp precision."""
    selected_feed = feed or stock_data_policy_from_env().feed
    if not symbols:
        raise ValueError("at least one symbol is required")
    if start_utc.tzinfo is None or end_utc.tzinfo is None or end_utc <= start_utc:
        raise ValueError("a valid timezone-aware quote interval is required")
    rows = _remote_rows(
        "quotes",
        symbols=symbols,
        start_utc=start_utc,
        end_utc=end_utc,
        client=client,
    )
    for row in rows:
        for name in ("bid_price", "ask_price", "bid_size", "ask_size"):
            row[name] = nullable_float(row.get(name))
        row["source"] = "cloud.alpaca.market_data"
        row["feed"] = selected_feed

    frame = pl.DataFrame(rows) if rows else _empty_quotes()
    return _canonicalize_quotes(frame).filter(
        (pl.col("ts_utc") >= start_utc) & (pl.col("ts_utc") < end_utc)
    )


def fetch_trades(
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
    *,
    feed: AlpacaStockFeed | None = None,
    client: httpx.Client | None = None,
) -> pl.DataFrame:
    """Fetch every historical SIP trade print for point-in-time order-flow research."""

    selected_feed = feed or stock_data_policy_from_env().feed
    if not symbols:
        raise ValueError("at least one symbol is required")
    if start_utc.tzinfo is None or end_utc.tzinfo is None or end_utc <= start_utc:
        raise ValueError("a valid timezone-aware trade interval is required")
    rows = _remote_rows(
        "trades",
        symbols=symbols,
        start_utc=start_utc,
        end_utc=end_utc,
        client=client,
    )
    for row in rows:
        row["price"] = nullable_float(row.get("price"))
        row["size"] = nullable_float(row.get("size"))
        row["source"] = "cloud.alpaca.market_data"
        row["feed"] = selected_feed
    frame = pl.DataFrame(rows) if rows else _empty_trades()
    return _canonicalize_trades(frame).filter(
        (pl.col("ts_utc") >= start_utc) & (pl.col("ts_utc") < end_utc)
    )


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


def _empty_quotes() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "symbol": pl.String,
            "ts_utc": pl.Datetime("ns", "UTC"),
            "bid_price": pl.Float64,
            "ask_price": pl.Float64,
            "bid_size": pl.Float64,
            "ask_size": pl.Float64,
            "bid_exchange": pl.String,
            "ask_exchange": pl.String,
            "conditions": pl.List(pl.String),
            "tape": pl.String,
            "source": pl.String,
            "feed": pl.String,
        }
    )


def _empty_trades() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "symbol": pl.String,
            "ts_utc": pl.Datetime("ns", "UTC"),
            "trade_id": pl.Int64,
            "exchange": pl.String,
            "price": pl.Float64,
            "size": pl.Int64,
            "conditions": pl.List(pl.String),
            "tape": pl.String,
            "source": pl.String,
            "feed": pl.String,
        }
    )


def _canonicalize_quotes(frame: pl.DataFrame) -> pl.DataFrame:
    columns = _empty_quotes().columns
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f"quotes missing required columns: {sorted(missing)}")
    timestamp = pl.col("ts_utc")
    if frame.schema["ts_utc"] == pl.String:
        timestamp = timestamp.str.to_datetime(time_unit="ns", time_zone="UTC", strict=False)
    return (
        frame.select(columns)
        .with_columns(
            pl.col("symbol").cast(pl.String),
            timestamp.cast(pl.Datetime("ns", "UTC")).alias("ts_utc"),
            pl.col("bid_price").cast(pl.Float64),
            pl.col("ask_price").cast(pl.Float64),
            pl.col("bid_size").cast(pl.Float64),
            pl.col("ask_size").cast(pl.Float64),
            pl.col("conditions").cast(pl.List(pl.String)),
        )
        .sort("symbol", "ts_utc")
    )


def _canonicalize_trades(frame: pl.DataFrame) -> pl.DataFrame:
    columns = _empty_trades().columns
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f"trades missing required columns: {sorted(missing)}")
    timestamp = pl.col("ts_utc")
    if frame.schema["ts_utc"] == pl.String:
        timestamp = timestamp.str.to_datetime(
            time_unit="ns",
            time_zone="UTC",
            strict=False,
        )
    return (
        frame.select(columns)
        .with_columns(
            pl.col("symbol").cast(pl.String),
            timestamp.cast(pl.Datetime("ns", "UTC")).alias("ts_utc"),
            pl.col("trade_id").cast(pl.Int64),
            pl.col("exchange").cast(pl.String),
            pl.col("price").cast(pl.Float64),
            pl.col("size").cast(pl.Int64),
            pl.col("conditions").cast(pl.List(pl.String)),
            pl.col("tape").cast(pl.String),
        )
        .sort("symbol", "ts_utc", "trade_id")
    )
