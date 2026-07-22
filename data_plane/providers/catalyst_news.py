from __future__ import annotations

import time
from datetime import UTC, datetime

import polars as pl

from data_plane.catalysts import canonicalize_catalysts, empty_catalyst_frame
from data_plane.http import DownloadError, QueryValue, get_json
from data_plane.providers.alpaca import credentials_from_env
from data_plane.providers.massive import _set_query_value, api_key_from_env

ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
MASSIVE_NEWS_URL = "https://api.massive.com/v2/reference/news"


def fetch_alpaca_news(start_utc: datetime, end_utc: datetime) -> pl.DataFrame:
    _validate_window(start_utc, end_utc)
    key, secret = credentials_from_env()
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    params: dict[str, QueryValue] = {
        "start": start_utc.isoformat(),
        "end": end_utc.isoformat(),
        "sort": "asc",
        "limit": 50,
        "include_content": "false",
    }
    retrieved = datetime.now(UTC)
    rows: list[dict[str, object]] = []
    while True:
        payload = get_json(ALPACA_NEWS_URL, params=params, headers=headers)
        articles = payload.get("news", [])
        if not isinstance(articles, list):
            raise DownloadError("Alpaca news response did not contain a news list")
        for article in articles:
            if not isinstance(article, dict):
                continue
            symbols = article.get("symbols", [])
            if not isinstance(symbols, list) or not symbols:
                continue
            event_id = str(article.get("id", "")).strip()
            if not event_id:
                continue
            rows.append(
                {
                    "source": "alpaca.news.benzinga",
                    "source_event_id": event_id,
                    "event_type": "news",
                    "event_subtype": None,
                    "published_utc": article.get("created_at"),
                    "updated_utc": article.get("updated_at"),
                    "retrieved_utc": retrieved,
                    "symbols": symbols,
                    "headline": article.get("headline"),
                    "summary": article.get("summary"),
                    "publisher": article.get("source") or "Benzinga",
                    "url": article.get("url"),
                    "cik": None,
                    "accession_number": None,
                    "form_items": [],
                    "tags": [],
                    "provenance": f"alpaca.news:{event_id}",
                }
            )
        token = payload.get("next_page_token")
        if not token:
            break
        params["page_token"] = str(token)

    frame = canonicalize_catalysts(pl.DataFrame(rows) if rows else empty_catalyst_frame())
    return frame.filter(
        (pl.col("published_utc") >= start_utc) & (pl.col("published_utc") < end_utc)
    )


def fetch_massive_news(
    start_utc: datetime,
    end_utc: datetime,
    *,
    pace_seconds: float = 12.5,
) -> pl.DataFrame:
    _validate_window(start_utc, end_utc)
    if pace_seconds < 0:
        raise ValueError("pace_seconds must be nonnegative")
    headers = {"Authorization": f"Bearer {api_key_from_env()}"}
    url = MASSIVE_NEWS_URL
    params: dict[str, QueryValue] | None = {
        "published_utc.gte": start_utc.isoformat(),
        "published_utc.lt": end_utc.isoformat(),
        "sort": "published_utc",
        "order": "asc",
        "limit": 1000,
    }
    retrieved = datetime.now(UTC)
    rows: list[dict[str, object]] = []
    previous_request_started = 0.0
    seen_urls: set[str] = set()
    while True:
        elapsed = time.monotonic() - previous_request_started
        if previous_request_started and elapsed < pace_seconds:
            time.sleep(pace_seconds - elapsed)
        previous_request_started = time.monotonic()
        payload = get_json(url, params=params, headers=headers)
        articles = payload.get("results", [])
        if not isinstance(articles, list):
            raise DownloadError("Massive news response did not contain a result list")
        for article in articles:
            if not isinstance(article, dict):
                continue
            symbols = article.get("tickers", [])
            if not isinstance(symbols, list) or not symbols:
                continue
            event_id = str(article.get("id", "")).strip()
            if not event_id:
                continue
            publisher = article.get("publisher")
            publisher_name = publisher.get("name") if isinstance(publisher, dict) else None
            keywords = article.get("keywords", [])
            rows.append(
                {
                    "source": "massive.news",
                    "source_event_id": event_id,
                    "event_type": "news",
                    "event_subtype": None,
                    "published_utc": article.get("published_utc"),
                    "updated_utc": None,
                    "retrieved_utc": retrieved,
                    "symbols": symbols,
                    "headline": article.get("title"),
                    "summary": article.get("description"),
                    "publisher": publisher_name,
                    "url": article.get("article_url"),
                    "cik": None,
                    "accession_number": None,
                    "form_items": [],
                    "tags": keywords if isinstance(keywords, list) else [],
                    "provenance": f"massive.news:{event_id}",
                }
            )
        next_url = payload.get("next_url")
        if not next_url:
            break
        next_url_text = str(next_url)
        if next_url_text in seen_urls:
            raise DownloadError("Massive news pagination returned a repeated next_url")
        seen_urls.add(next_url_text)
        url = _set_query_value(next_url_text, "limit", "1000")
        params = None

    frame = canonicalize_catalysts(pl.DataFrame(rows) if rows else empty_catalyst_frame())
    return frame.filter(
        (pl.col("published_utc") >= start_utc) & (pl.col("published_utc") < end_utc)
    )


def _validate_window(start_utc: datetime, end_utc: datetime) -> None:
    if start_utc.tzinfo is None or end_utc.tzinfo is None:
        raise ValueError("news bounds must be timezone-aware")
    if end_utc <= start_utc:
        raise ValueError("news end must be after start")
