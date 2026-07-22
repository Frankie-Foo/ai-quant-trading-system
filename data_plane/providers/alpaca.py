from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import polars as pl

from data_plane.http import DownloadError, QueryValue, get_json
from data_plane.quality import canonicalize_bars, nullable_float

BASE_URL = "https://data.alpaca.markets/v2/stocks/bars"
QUOTES_URL = "https://data.alpaca.markets/v2/stocks/quotes"
AlpacaStockFeed = Literal["sip", "delayed_sip"]


@dataclass(frozen=True)
class AlpacaStockDataPolicy:
    """Licensed SIP freshness policy shared by every REST market-data consumer."""

    feed: AlpacaStockFeed
    delay_minutes: int
    is_realtime: bool


def stock_data_policy_from_env() -> AlpacaStockDataPolicy:
    """Resolve Alpaca feed semantics without silently inventing a delay."""

    raw = os.getenv("ALPACA_MARKET_DATA_FEED", "sip").strip().lower()
    if raw == "sip":
        return AlpacaStockDataPolicy(feed="sip", delay_minutes=0, is_realtime=True)
    if raw == "delayed_sip":
        return AlpacaStockDataPolicy(
            feed="delayed_sip",
            delay_minutes=15,
            is_realtime=False,
        )
    raise RuntimeError(
        "ALPACA_MARKET_DATA_FEED must be 'sip' or 'delayed_sip'; "
        "IEX is not accepted for full-market decisions"
    )


def credentials_from_env() -> tuple[str, str]:
    key = os.getenv("ALPACA_API_KEY_ID", "").strip()
    secret = os.getenv("ALPACA_API_SECRET_KEY", "").strip()
    if not key or not secret:
        raise RuntimeError(
            "missing ALPACA_API_KEY_ID/ALPACA_API_SECRET_KEY; create a free Alpaca "
            "account and put both values in the local .env file"
        )
    return key, secret


def fetch_bars(
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
    *,
    feed: AlpacaStockFeed | None = None,
) -> pl.DataFrame:
    key, secret = credentials_from_env()
    selected_feed = feed or stock_data_policy_from_env().feed
    if not symbols:
        raise ValueError("at least one symbol is required")
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    params: dict[str, QueryValue] = {
        "symbols": ",".join(symbols),
        "timeframe": "1Min",
        "start": start_utc.isoformat(),
        "end": end_utc.isoformat(),
        "feed": selected_feed,
        "adjustment": "split",
        "limit": 10_000,
        "sort": "asc",
    }
    rows: list[dict[str, object]] = []
    while True:
        payload = get_json(BASE_URL, params=params, headers=headers)
        bars = payload.get("bars", {})
        if not isinstance(bars, dict):
            raise DownloadError("Alpaca bars response did not contain a symbol map")
        for symbol, symbol_bars in bars.items():
            if not isinstance(symbol_bars, list):
                continue
            for bar in symbol_bars:
                if not isinstance(bar, dict):
                    continue
                rows.append(
                    {
                        "symbol": symbol,
                        "ts_utc": bar.get("t"),
                        "open": bar.get("o"),
                        "high": bar.get("h"),
                        "low": bar.get("l"),
                        "close": bar.get("c"),
                        "volume": bar.get("v"),
                        "trade_count": bar.get("n"),
                        "vwap": nullable_float(bar.get("vw")),
                        "source": "alpaca.market_data",
                        "feed": selected_feed,
                        "adjustment": "split_adjusted",
                    }
                )
        token = payload.get("next_page_token")
        if not token:
            break
        params["page_token"] = str(token)

    frame = pl.DataFrame(rows) if rows else _empty_frame()
    return canonicalize_bars(frame).filter(
        (pl.col("ts_utc") >= start_utc) & (pl.col("ts_utc") < end_utc)
    )


def fetch_quotes(
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
    *,
    feed: AlpacaStockFeed | None = None,
) -> pl.DataFrame:
    """Fetch historical SIP NBBO quote updates without reducing timestamp precision."""
    key, secret = credentials_from_env()
    selected_feed = feed or stock_data_policy_from_env().feed
    if not symbols:
        raise ValueError("at least one symbol is required")
    if start_utc.tzinfo is None or end_utc.tzinfo is None or end_utc <= start_utc:
        raise ValueError("a valid timezone-aware quote interval is required")
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    params: dict[str, QueryValue] = {
        "symbols": ",".join(symbols),
        "start": start_utc.isoformat(),
        "end": end_utc.isoformat(),
        "feed": selected_feed,
        "limit": 10_000,
        "sort": "asc",
    }
    rows: list[dict[str, object]] = []
    while True:
        payload = get_json(QUOTES_URL, params=params, headers=headers)
        quotes = payload.get("quotes", {})
        if not isinstance(quotes, dict):
            raise DownloadError("Alpaca quotes response did not contain a symbol map")
        for symbol, symbol_quotes in quotes.items():
            if not isinstance(symbol_quotes, list):
                continue
            for quote in symbol_quotes:
                if not isinstance(quote, dict):
                    continue
                conditions = quote.get("c")
                rows.append(
                    {
                        "symbol": symbol,
                        "ts_utc": quote.get("t"),
                        "bid_price": nullable_float(quote.get("bp")),
                        "ask_price": nullable_float(quote.get("ap")),
                        "bid_size": nullable_float(quote.get("bs")),
                        "ask_size": nullable_float(quote.get("as")),
                        "bid_exchange": quote.get("bx"),
                        "ask_exchange": quote.get("ax"),
                        "conditions": (
                            [str(item) for item in conditions]
                            if isinstance(conditions, list)
                            else []
                        ),
                        "tape": quote.get("z"),
                        "source": "alpaca.market_data",
                        "feed": selected_feed,
                    }
                )
        token = payload.get("next_page_token")
        if not token:
            break
        params["page_token"] = str(token)

    frame = pl.DataFrame(rows) if rows else _empty_quotes()
    return _canonicalize_quotes(frame).filter(
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
