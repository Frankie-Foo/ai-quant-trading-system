from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx
import polars as pl
from pydantic import SecretStr

from data_plane.http import DownloadError
from data_plane.providers.alpaca_direct import (
    DirectAlpacaMarketDataClient,
    DirectMarketDataError,
)
from data_plane.quality import canonicalize_bars, nullable_float

PLATFORM_API_VERSION = "v1"
AlpacaStockFeed = Literal["sip", "delayed_sip"]
MarketDataProvider = Literal["alpaca_direct", "cloud_proxy"]


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


def market_data_provider_from_env() -> MarketDataProvider:
    """Use direct Alpaca SIP unless the cloud proxy is explicitly selected."""

    raw = os.getenv("MARKET_DATA_PROVIDER", "alpaca_direct").strip().lower()
    if raw == "alpaca_direct":
        return "alpaca_direct"
    if raw == "cloud_proxy":
        return "cloud_proxy"
    raise RuntimeError(
        "MARKET_DATA_PROVIDER must be 'alpaca_direct' or 'cloud_proxy'"
    )


def _direct_credentials() -> tuple[SecretStr, SecretStr]:
    key_id = _first_present("ALPACA_API_KEY_ID", "ALPACA_PAPER_KEY_ID")
    secret_key = _first_present("ALPACA_API_SECRET_KEY", "ALPACA_PAPER_SECRET_KEY")
    if key_id is None or secret_key is None:
        raise DownloadError(
            "direct Alpaca market-data credentials are required"
        )
    return SecretStr(key_id), SecretStr(secret_key)


def _first_present(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None


def _direct_client(feed: AlpacaStockFeed) -> DirectAlpacaMarketDataClient:
    if feed != "sip":
        raise RuntimeError(
            "direct Alpaca market data requires CLOUD_MARKET_DATA_FEED=sip"
        )
    key_id, secret_key = _direct_credentials()
    return DirectAlpacaMarketDataClient(key_id=key_id, secret_key=secret_key)


def _use_direct_provider(client: httpx.Client | None) -> bool:
    return client is None and market_data_provider_from_env() == "alpaca_direct"


def _direct_bars(
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
    *,
    feed: AlpacaStockFeed,
) -> pl.DataFrame:
    client = _direct_client(feed)
    try:
        events = client.fetch_bars(
            symbols,
            start_utc=start_utc,
            end_utc=end_utc,
        )
    except (DirectMarketDataError, ValueError) as exc:
        raise DownloadError("direct Alpaca market-data request failed") from exc
    finally:
        client.close()
    rows: list[dict[str, object]] = [
        {
            "symbol": event.symbol,
            "ts_utc": event.ts_utc,
            "open": event.open,
            "high": event.high,
            "low": event.low,
            "close": event.close,
            "volume": event.volume,
            "trade_count": event.trade_count,
            "vwap": event.vwap,
            "source": "alpaca.sip.rest.bars",
            "feed": event.feed,
            "adjustment": "split_adjusted",
        }
        for event in events
    ]
    frame = pl.DataFrame(rows) if rows else _empty_frame()
    return canonicalize_bars(frame).filter(
        (pl.col("ts_utc") >= start_utc) & (pl.col("ts_utc") < end_utc)
    )


def _direct_quotes(
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
    *,
    feed: AlpacaStockFeed,
) -> pl.DataFrame:
    client = _direct_client(feed)
    try:
        events = client.fetch_quotes(
            symbols,
            start_utc=start_utc,
            end_utc=end_utc,
        )
    except (DirectMarketDataError, ValueError) as exc:
        raise DownloadError("direct Alpaca market-data request failed") from exc
    finally:
        client.close()
    rows: list[dict[str, object]] = [
        {
            "symbol": event.symbol,
            "ts_utc": event.ts_utc,
            "bid_price": event.bid_price,
            "ask_price": event.ask_price,
            "bid_size": event.bid_size,
            "ask_size": event.ask_size,
            "bid_exchange": None,
            "ask_exchange": None,
            "conditions": [],
            "tape": None,
            "source": "alpaca.sip.rest.quotes",
            "feed": event.feed,
        }
        for event in events
    ]
    frame = pl.DataFrame(rows) if rows else _empty_quotes()
    return _canonicalize_quotes(frame).filter(
        (pl.col("ts_utc") >= start_utc) & (pl.col("ts_utc") < end_utc)
    )


def _direct_trades(
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
    *,
    feed: AlpacaStockFeed,
) -> pl.DataFrame:
    client = _direct_client(feed)
    try:
        events = client.fetch_trades(
            symbols,
            start_utc=start_utc,
            end_utc=end_utc,
        )
    except (DirectMarketDataError, ValueError) as exc:
        raise DownloadError("direct Alpaca market-data request failed") from exc
    finally:
        client.close()
    rows = [
        {
            "symbol": event.symbol,
            "ts_utc": event.ts_utc,
            "trade_id": event.trade_id,
            "exchange": event.exchange,
            "price": event.price,
            "size": event.size,
            "conditions": list(event.conditions),
            "tape": event.tape,
            "source": "alpaca.sip.rest.trades",
            "feed": event.feed,
        }
        for event in events
    ]
    frame = pl.DataFrame(rows) if rows else _empty_trades()
    return _canonicalize_trades(frame).filter(
        (pl.col("ts_utc") >= start_utc) & (pl.col("ts_utc") < end_utc)
    )


def _direct_coverage(
    symbols: tuple[str, ...],
    frame: pl.DataFrame,
) -> dict[str, Any]:
    returned = set(frame.get_column("symbol").unique().to_list())
    symbol_coverage = [
        {
            "symbol": symbol,
            "status": "observed" if symbol in returned else "empty",
            "reason_codes": (
                ["direct_alpaca_sip"]
                if symbol in returned
                else ["no_observed_bars"]
            ),
        }
        for symbol in sorted(set(symbols))
    ]
    complete_symbols = len(returned) == len(set(symbols))
    return {
        "status": "observed" if complete_symbols else "gaps_detected",
        "fallback_recommended": not complete_symbols,
        "symbols": symbol_coverage,
        "provenance": "alpaca.sip.rest.bars",
    }


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
    if _use_direct_provider(client):
        return _direct_bars(symbols, start_utc, end_utc, feed=selected_feed)
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
    if _use_direct_provider(client):
        frame = _direct_bars(symbols, start_utc, end_utc, feed=selected_feed)
        if frame.is_empty():
            raise DownloadError("direct Alpaca market-data API returned no observed bars")
        return frame, _direct_coverage(symbols, frame)
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
    if _use_direct_provider(client):
        return _direct_quotes(symbols, start_utc, end_utc, feed=selected_feed)
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
    if _use_direct_provider(client):
        return _direct_trades(symbols, start_utc, end_utc, feed=selected_feed)
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
