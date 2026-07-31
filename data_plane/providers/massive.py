from __future__ import annotations

import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import polars as pl

from data_plane.daily import canonicalize_daily_bars
from data_plane.http import DownloadError, QueryValue, get_json
from data_plane.quality import canonicalize_bars, nullable_float

BASE_URL = "https://api.massive.com/v2/aggs/ticker"
GROUPED_DAILY_URL = "https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks"
TICKER_REFERENCE_URL = "https://api.massive.com/v3/reference/tickers"
FREE_FLOAT_URL = "https://api.massive.com/stocks/vX/float"


def _set_query_value(url: str, name: str, value: str) -> str:
    """Set one query value without discarding the provider's opaque cursor."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[name] = value
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def api_key_from_env() -> str:
    key = os.getenv("MASSIVE_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "missing MASSIVE_API_KEY; create a Massive Basic account and put the key "
            "in the local .env file"
        )
    return key


def fetch_bars(
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
) -> pl.DataFrame:
    key = api_key_from_env()
    headers = {"Authorization": f"Bearer {key}"}
    rows: list[dict[str, object]] = []
    for symbol in symbols:
        url = f"{BASE_URL}/{symbol}/range/1/minute/{start_utc.date()}/{end_utc.date()}"
        params: dict[str, QueryValue] | None = {
            "adjusted": "true",
            "sort": "asc",
            "limit": 50_000,
        }
        while True:
            payload = get_json(url, params=params, headers=headers)
            results = payload.get("results", [])
            if not isinstance(results, list):
                raise DownloadError("Massive aggregate response did not contain a result list")
            for bar in results:
                if not isinstance(bar, dict):
                    continue
                rows.append(
                    {
                        "symbol": symbol,
                        "ts_utc": datetime.fromtimestamp(float(bar["t"]) / 1000, UTC),
                        "open": bar.get("o"),
                        "high": bar.get("h"),
                        "low": bar.get("l"),
                        "close": bar.get("c"),
                        "volume": bar.get("v"),
                        "trade_count": bar.get("n"),
                        "vwap": nullable_float(bar.get("vw")),
                        "source": "massive.aggregates",
                        "feed": "sip",
                        "adjustment": "split_adjusted",
                    }
                )
            next_url = payload.get("next_url")
            if not next_url:
                break
            url = str(next_url)
            params = None

    frame = pl.DataFrame(rows) if rows else _empty_frame()
    return canonicalize_bars(frame).filter(
        (pl.col("ts_utc") >= start_utc) & (pl.col("ts_utc") < end_utc)
    )


def fetch_grouped_daily(trade_date: date) -> pl.DataFrame:
    key = api_key_from_env()
    payload = get_json(
        f"{GROUPED_DAILY_URL}/{trade_date.isoformat()}",
        params={"adjusted": "true", "include_otc": "false"},
        headers={"Authorization": f"Bearer {key}"},
    )
    results = payload.get("results", [])
    if not isinstance(results, list):
        raise DownloadError("Massive grouped-daily response did not contain a result list")
    rows: list[dict[str, object]] = []
    for bar in results:
        if not isinstance(bar, dict) or "T" not in bar or "t" not in bar:
            continue
        rows.append(
            {
                "symbol": bar["T"],
                "trade_date": trade_date,
                "provider_ts_utc": datetime.fromtimestamp(float(bar["t"]) / 1000, UTC),
                "open": bar.get("o"),
                "high": bar.get("h"),
                "low": bar.get("l"),
                "close": bar.get("c"),
                "volume": bar.get("v"),
                "trade_count": bar.get("n"),
                "vwap": nullable_float(bar.get("vw")),
                "source": "massive.grouped_daily",
                "feed": "sip_excluding_otc",
                "adjustment": "split_adjusted",
            }
        )
    return canonicalize_daily_bars(pl.DataFrame(rows) if rows else _empty_daily_frame())


def fetch_ticker_reference(
    asof_date: date,
    *,
    active: bool,
    security_type: str | None = "CS",
    pace_seconds: float = 12.5,
    on_page: Callable[[int, int], None] | None = None,
) -> pl.DataFrame:
    key = api_key_from_env()
    headers = {"Authorization": f"Bearer {key}"}
    url = TICKER_REFERENCE_URL
    initial_params: dict[str, QueryValue] = {
        "market": "stocks",
        "locale": "us",
        "date": asof_date.isoformat(),
        "active": str(active).lower(),
        "limit": 1000,
        "sort": "ticker",
        "order": "asc",
    }
    if security_type:
        initial_params["type"] = security_type
    params: dict[str, QueryValue] | None = initial_params
    rows: list[dict[str, object]] = []
    previous_request_started = 0.0
    seen_urls: set[str] = set()
    page = 0
    while True:
        elapsed = time.monotonic() - previous_request_started
        if previous_request_started and elapsed < pace_seconds:
            time.sleep(pace_seconds - elapsed)
        previous_request_started = time.monotonic()
        payload = get_json(url, params=params, headers=headers)
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise DownloadError("Massive ticker response did not contain a result list")
        for item in results:
            if not isinstance(item, dict) or not item.get("ticker"):
                continue
            rows.append(
                {
                    "asof_date": asof_date,
                    "symbol": item["ticker"],
                    "name": item.get("name"),
                    "market": item.get("market"),
                    "locale": item.get("locale"),
                    "primary_exchange": item.get("primary_exchange"),
                    "security_type": item.get("type"),
                    "active": item.get("active"),
                    "currency_name": item.get("currency_name"),
                    "cik": item.get("cik"),
                    "composite_figi": item.get("composite_figi"),
                    "share_class_figi": item.get("share_class_figi"),
                    "last_updated_utc": item.get("last_updated_utc"),
                    "source": "massive.reference_tickers",
                }
            )
        page += 1
        if on_page:
            on_page(page, len(rows))
        next_url = payload.get("next_url")
        if not next_url:
            break
        next_url_text = str(next_url)
        if next_url_text in seen_urls:
            raise DownloadError("Massive ticker pagination returned a repeated next_url")
        seen_urls.add(next_url_text)
        # Massive's next_url contains only its opaque cursor. Reasserting the requested
        # page size avoids silently falling back to the provider's smaller default.
        url = _set_query_value(next_url_text, "limit", "1000")
        params = None
    return _canonicalize_reference(pl.DataFrame(rows) if rows else _empty_reference_frame())


def fetch_ticker_details(
    symbols: tuple[str, ...],
    asof_date: date,
    *,
    pace_seconds: float = 12.5,
    on_symbol: Callable[[int, int], None] | None = None,
) -> pl.DataFrame:
    """Fetch point-in-time market-cap fields for an explicit symbol set."""
    key = api_key_from_env()
    headers = {"Authorization": f"Bearer {key}"}
    requested = tuple(sorted(set(symbols)))
    def fetch_one(symbol: str) -> dict[str, object]:
        try:
            payload = get_json(
                f"{TICKER_REFERENCE_URL}/{symbol}",
                params={"date": asof_date.isoformat()},
                headers=headers,
                attempts=1,
                timeout_seconds=8.0,
            )
            item = payload.get("results")
            values = item if isinstance(item, dict) else {}
        except DownloadError:
            values = {}
        return {
                "asof_date": asof_date,
                "symbol": symbol,
                "market_cap": nullable_float(values.get("market_cap")),
                "weighted_shares_outstanding": nullable_float(
                    values.get("weighted_shares_outstanding")
                ),
                "share_class_shares_outstanding": nullable_float(
                    values.get("share_class_shares_outstanding")
                ),
                "security_type": values.get("type"),
                "active": values.get("active"),
                "cik": values.get("cik"),
                "last_updated_utc": values.get("last_updated_utc"),
                "retrieved_utc": datetime.now(UTC),
                "source": "massive.ticker_details",
                "provenance": (
                    f"massive.ticker_details:{symbol}@{asof_date.isoformat()}"
                ),
            }

    rows: list[dict[str, object]] = []
    if pace_seconds < 1 and len(requested) > 1:
        with ThreadPoolExecutor(max_workers=min(12, len(requested))) as executor:
            for index, row in enumerate(executor.map(fetch_one, requested), start=1):
                rows.append(row)
                if on_symbol:
                    on_symbol(index, len(requested))
    else:
        previous_request_started = 0.0
        for index, symbol in enumerate(requested, start=1):
            elapsed = time.monotonic() - previous_request_started
            if previous_request_started and elapsed < pace_seconds:
                time.sleep(pace_seconds - elapsed)
            previous_request_started = time.monotonic()
            rows.append(fetch_one(symbol))
            if on_symbol:
                on_symbol(index, len(requested))
    frame = pl.DataFrame(rows) if rows else empty_ticker_details_frame()
    return _canonicalize_ticker_details(frame)


def fetch_free_float(symbols: tuple[str, ...]) -> pl.DataFrame:
    """Fetch the latest provider free-float table and retain requested symbols."""
    key = api_key_from_env()
    headers = {"Authorization": f"Bearer {key}"}
    url = FREE_FLOAT_URL
    params: dict[str, QueryValue] | None = {
        "limit": 5000,
        "sort": "ticker.asc",
    }
    requested = set(symbols)
    rows: list[dict[str, object]] = []
    seen_urls: set[str] = set()
    while True:
        payload = get_json(url, params=params, headers=headers)
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise DownloadError("Massive free-float response did not contain a result list")
        retrieved_utc = datetime.now(UTC)
        for item in results:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("ticker") or "").strip().upper()
            if symbol not in requested:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "effective_date": item.get("effective_date"),
                    "free_float": nullable_float(item.get("free_float")),
                    "free_float_percent": nullable_float(
                        item.get("free_float_percent")
                    ),
                    "retrieved_utc": retrieved_utc,
                    "source": "massive.free_float",
                    "provenance": f"massive.free_float:{symbol}",
                }
            )
        next_url = payload.get("next_url")
        if not next_url:
            break
        next_text = str(next_url)
        if next_text in seen_urls:
            raise DownloadError("Massive free-float pagination returned a repeated next_url")
        seen_urls.add(next_text)
        url = _set_query_value(next_text, "limit", "5000")
        params = None
    frame = pl.DataFrame(rows) if rows else empty_free_float_frame()
    return frame.with_columns(
        pl.col("effective_date").cast(pl.String).str.to_date(strict=False),
        pl.col("retrieved_utc").cast(pl.Datetime("ms", "UTC")),
    ).sort("symbol")


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


def _empty_daily_frame() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "symbol": pl.String,
            "trade_date": pl.Date,
            "provider_ts_utc": pl.Datetime("ms", "UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Float64,
            "trade_count": pl.Int64,
            "vwap": pl.Float64,
            "source": pl.String,
            "feed": pl.String,
            "adjustment": pl.String,
        }
    )


def _canonicalize_reference(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_columns(
        pl.col("asof_date").cast(pl.Date),
        pl.col("symbol").cast(pl.String),
        pl.col("active").cast(pl.Boolean),
        pl.col("last_updated_utc").cast(pl.String).str.to_datetime(
            time_zone="UTC", strict=False
        ),
    ).sort("symbol")


def _empty_reference_frame() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "asof_date": pl.Date,
            "symbol": pl.String,
            "name": pl.String,
            "market": pl.String,
            "locale": pl.String,
            "primary_exchange": pl.String,
            "security_type": pl.String,
            "active": pl.Boolean,
            "currency_name": pl.String,
            "cik": pl.String,
            "composite_figi": pl.String,
            "share_class_figi": pl.String,
            "last_updated_utc": pl.Datetime("ms", "UTC"),
            "source": pl.String,
        }
    )


def _canonicalize_ticker_details(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_columns(
        pl.col("asof_date").cast(pl.Date),
        pl.col("symbol").cast(pl.String),
        pl.col("market_cap").cast(pl.Float64),
        pl.col("weighted_shares_outstanding").cast(pl.Float64),
        pl.col("share_class_shares_outstanding").cast(pl.Float64),
        pl.col("active").cast(pl.Boolean),
        pl.col("last_updated_utc").cast(pl.String).str.to_datetime(
            time_zone="UTC", strict=False
        ),
        pl.col("retrieved_utc").cast(pl.Datetime("ms", "UTC")),
    ).sort("symbol")


def empty_ticker_details_frame() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "asof_date": pl.Date,
            "symbol": pl.String,
            "market_cap": pl.Float64,
            "weighted_shares_outstanding": pl.Float64,
            "share_class_shares_outstanding": pl.Float64,
            "security_type": pl.String,
            "active": pl.Boolean,
            "cik": pl.String,
            "last_updated_utc": pl.Datetime("ms", "UTC"),
            "retrieved_utc": pl.Datetime("ms", "UTC"),
            "source": pl.String,
            "provenance": pl.String,
        }
    )


def empty_free_float_frame() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "symbol": pl.String,
            "effective_date": pl.Date,
            "free_float": pl.Float64,
            "free_float_percent": pl.Float64,
            "retrieved_utc": pl.Datetime("ms", "UTC"),
            "source": pl.String,
            "provenance": pl.String,
        }
    )
