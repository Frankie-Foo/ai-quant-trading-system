from __future__ import annotations

import re
import time
from datetime import UTC, datetime

import httpx
import polars as pl

from data_plane.catalysts import canonicalize_catalysts, empty_catalyst_frame
from data_plane.http import DownloadError, QueryValue, get_json
from data_plane.providers.alpaca import PLATFORM_API_VERSION, platform_access_from_env
from data_plane.providers.massive import _set_query_value, api_key_from_env

MASSIVE_NEWS_URL = "https://api.massive.com/v2/reference/news"


def fetch_alpaca_news(
    start_utc: datetime,
    end_utc: datetime,
    *,
    client: httpx.Client | None = None,
) -> pl.DataFrame:
    _validate_window(start_utc, end_utc)
    base_url, token = platform_access_from_env()
    owns_client = client is None
    http_client = client or httpx.Client(timeout=30)
    try:
        response = http_client.get(
            f"{base_url}/{PLATFORM_API_VERSION}/market-data/news",
            params={"start": start_utc.isoformat(), "end": end_utc.isoformat()},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise DownloadError("cloud news API request failed") from exc
    finally:
        if owns_client:
            http_client.close()
    if not isinstance(payload, dict) or payload.get("api_version") != PLATFORM_API_VERSION:
        raise DownloadError("cloud news API contract is invalid")
    articles = payload.get("news", [])
    if not isinstance(articles, list):
        raise DownloadError("cloud news response did not contain a news list")
    retrieved = datetime.now(UTC)
    rows: list[dict[str, object]] = []
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
                "provenance": f"cloud.alpaca.news:{event_id}",
            }
        )

    frame = canonicalize_catalysts(pl.DataFrame(rows) if rows else empty_catalyst_frame())
    return frame.filter(
        (pl.col("published_utc") >= start_utc) & (pl.col("published_utc") < end_utc)
    )


def fetch_massive_news(
    start_utc: datetime,
    end_utc: datetime,
    *,
    pace_seconds: float = 12.5,
    ticker: str | None = None,
) -> pl.DataFrame:
    _validate_window(start_utc, end_utc)
    if pace_seconds < 0:
        raise ValueError("pace_seconds must be nonnegative")
    normalized_ticker = None if ticker is None else ticker.strip().upper()
    if normalized_ticker is not None and (
        not normalized_ticker
        or re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", normalized_ticker) is None
    ):
        raise ValueError("Massive news ticker is invalid")
    headers = {"Authorization": f"Bearer {api_key_from_env()}"}
    url = MASSIVE_NEWS_URL
    params: dict[str, QueryValue] | None = {
        "published_utc.gte": start_utc.isoformat(),
        "published_utc.lt": end_utc.isoformat(),
        "sort": "published_utc",
        "order": "asc",
        "limit": 1000,
        **({"ticker": normalized_ticker} if normalized_ticker else {}),
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
