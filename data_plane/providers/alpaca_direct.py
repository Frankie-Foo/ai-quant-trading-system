"""Temporary direct Alpaca SIP REST fallback for local Paper validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast

import httpx
from pydantic import SecretStr

from execution.alpaca_sip_stream import (
    SYMBOL_PATTERN,
    SipBar,
    SipQuote,
    SipTrade,
)

EndpointKind = Literal["bars", "quotes", "trades"]


class DirectMarketDataError(RuntimeError):
    """Sanitized direct market-data failure without credentials or bodies."""


@dataclass(frozen=True)
class AlpacaNewsArticle:
    article_id: str
    headline: str
    summary: str
    author: str
    created_at_utc: datetime
    updated_at_utc: datetime
    url: str
    symbols: tuple[str, ...]
    source: str

    def __post_init__(self) -> None:
        if not self.article_id.strip() or not self.headline.strip():
            raise ValueError("Alpaca news identity and headline are required")
        _window_timestamp(self.created_at_utc, name="news created_at_utc")
        _window_timestamp(self.updated_at_utc, name="news updated_at_utc")
        if self.updated_at_utc < self.created_at_utc:
            raise ValueError("Alpaca news update cannot predate creation")
        if not self.symbols or any(
            not SYMBOL_PATTERN.fullmatch(symbol) for symbol in self.symbols
        ):
            raise ValueError("Alpaca news symbols are invalid")
        if not self.source.strip():
            raise ValueError("Alpaca news source is required")

    @property
    def provenance(self) -> str:
        return f"alpaca.news.{self.source.lower()}:{self.article_id}"


class DirectAlpacaMarketDataClient:
    DATA_BASE_URL = "https://data.alpaca.markets"

    def __init__(
        self,
        *,
        key_id: SecretStr,
        secret_key: SecretStr,
        base_url: str = DATA_BASE_URL,
        client: httpx.Client | None = None,
        max_pages: int = 500,
    ):
        normalized = base_url.rstrip("/")
        if normalized != self.DATA_BASE_URL:
            raise ValueError("direct Alpaca market data must use the data host")
        if not key_id.get_secret_value().strip():
            raise ValueError("Alpaca market-data key ID is required")
        if not secret_key.get_secret_value().strip():
            raise ValueError("Alpaca market-data secret key is required")
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        self.base_url = normalized
        self.max_pages = max_pages
        self._headers = {
            "APCA-API-KEY-ID": key_id.get_secret_value(),
            "APCA-API-SECRET-KEY": secret_key.get_secret_value(),
        }
        self._client = client or httpx.Client(timeout=60.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch_bars(
        self,
        symbols: tuple[str, ...],
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[SipBar, ...]:
        rows = self._rows(
            "bars",
            symbols=symbols,
            start_utc=start_utc,
            end_utc=end_utc,
            extra={"timeframe": "1Min", "adjustment": "split"},
        )
        events: list[SipBar] = []
        for symbol, row in rows:
            try:
                ts_utc = _timestamp(row.get("t"))
                events.append(
                    SipBar.model_validate(
                        {
                            "symbol": symbol,
                            "ts_utc": ts_utc,
                            "open": row.get("o"),
                            "high": row.get("h"),
                            "low": row.get("l"),
                            "close": row.get("c"),
                            "volume": row.get("v"),
                            "trade_count": row.get("n"),
                            "vwap": row.get("vw"),
                            "provenance": (
                                f"alpaca.sip.rest.bars@{ts_utc.isoformat()}"
                            ),
                        }
                    )
                )
            except (TypeError, ValueError) as exc:
                raise DirectMarketDataError(
                    "Alpaca SIP bar failed schema validation"
                ) from exc
        return tuple(sorted(events, key=lambda item: (item.symbol, item.ts_utc)))

    def fetch_quotes(
        self,
        symbols: tuple[str, ...],
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[SipQuote, ...]:
        rows = self._rows(
            "quotes",
            symbols=symbols,
            start_utc=start_utc,
            end_utc=end_utc,
        )
        events: list[SipQuote] = []
        for symbol, row in rows:
            try:
                ts_utc = _timestamp(row.get("t"))
                events.append(
                    SipQuote.model_validate(
                        {
                            "symbol": symbol,
                            "ts_utc": ts_utc,
                            "bid_price": row.get("bp"),
                            "bid_size": row.get("bs"),
                            "ask_price": row.get("ap"),
                            "ask_size": row.get("as"),
                            "provenance": (
                                f"alpaca.sip.rest.quotes@{ts_utc.isoformat()}"
                            ),
                        }
                    )
                )
            except (TypeError, ValueError) as exc:
                raise DirectMarketDataError(
                    "Alpaca SIP quote failed schema validation"
                ) from exc
        return tuple(sorted(events, key=lambda item: (item.symbol, item.ts_utc)))

    def fetch_trades(
        self,
        symbols: tuple[str, ...],
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[SipTrade, ...]:
        rows = self._rows(
            "trades",
            symbols=symbols,
            start_utc=start_utc,
            end_utc=end_utc,
        )
        events: list[SipTrade] = []
        for symbol, row in rows:
            raw_conditions = row.get("c", [])
            try:
                ts_utc = _timestamp(row.get("t"))
                events.append(
                    SipTrade.model_validate(
                        {
                            "symbol": symbol,
                            "ts_utc": ts_utc,
                            "trade_id": row.get("i"),
                            "exchange": row.get("x"),
                            "price": row.get("p"),
                            "size": row.get("s"),
                            "conditions": (
                                tuple(str(item) for item in raw_conditions)
                                if isinstance(raw_conditions, list)
                                else ()
                            ),
                            "tape": row.get("z"),
                            "provenance": (
                                f"alpaca.sip.rest.trades@{ts_utc.isoformat()}"
                            ),
                        }
                    )
                )
            except (TypeError, ValueError) as exc:
                raise DirectMarketDataError(
                    "Alpaca SIP trade failed schema validation"
                ) from exc
        return tuple(sorted(events, key=lambda item: (item.symbol, item.ts_utc)))

    def fetch_news(
        self,
        symbols: tuple[str, ...],
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[AlpacaNewsArticle, ...]:
        normalized = _symbols(symbols)
        _window(start_utc, end_utc)
        params = {
            "symbols": ",".join(normalized),
            "start": start_utc.isoformat(),
            "end": end_utc.isoformat(),
            "sort": "asc",
            "limit": "50",
            "include_content": "false",
        }
        articles: list[AlpacaNewsArticle] = []
        token: str | None = None
        for _ in range(self.max_pages):
            page_params = dict(params)
            if token is not None:
                page_params["page_token"] = token
            payload = self._request_path(
                "/v1beta1/news",
                label="news",
                params=page_params,
            )
            rows = payload.get("news")
            if not isinstance(rows, list):
                raise DirectMarketDataError(
                    "Alpaca news response contract is invalid"
                )
            for raw in rows:
                if not isinstance(raw, dict):
                    raise DirectMarketDataError("Alpaca news row is invalid")
                values = cast(dict[str, Any], raw)
                raw_symbols = values.get("symbols")
                if not isinstance(raw_symbols, list):
                    raise DirectMarketDataError(
                        "Alpaca news symbols contract is invalid"
                    )
                article_symbols = tuple(
                    sorted(
                        {
                            str(symbol).strip().upper()
                            for symbol in raw_symbols
                            if str(symbol).strip().upper() in normalized
                        }
                    )
                )
                if not article_symbols:
                    continue
                try:
                    article = AlpacaNewsArticle(
                        article_id=str(values.get("id", "")),
                        headline=_required_text(values.get("headline")),
                        summary=_optional_text(values.get("summary")),
                        author=_optional_text(values.get("author")),
                        created_at_utc=_timestamp(values.get("created_at")),
                        updated_at_utc=_timestamp(values.get("updated_at")),
                        url=_optional_text(values.get("url")),
                        symbols=article_symbols,
                        source=_required_text(values.get("source")),
                    )
                except (TypeError, ValueError) as exc:
                    raise DirectMarketDataError(
                        "Alpaca news row failed schema validation"
                    ) from exc
                if (
                    article.created_at_utc < start_utc
                    or article.created_at_utc > end_utc
                    or article.updated_at_utc > end_utc
                ):
                    raise DirectMarketDataError(
                        "Alpaca news row violated the causal request window"
                    )
                articles.append(article)
            next_token = payload.get("next_page_token")
            if next_token is None:
                return tuple(
                    sorted(
                        articles,
                        key=lambda item: (
                            item.created_at_utc,
                            item.article_id,
                        ),
                    )
                )
            if not isinstance(next_token, str) or not next_token.strip():
                raise DirectMarketDataError(
                    "Alpaca news pagination token is invalid"
                )
            token = next_token
        raise DirectMarketDataError(
            "Alpaca news pagination exceeded the safety limit"
        )

    def _rows(
        self,
        kind: EndpointKind,
        *,
        symbols: tuple[str, ...],
        start_utc: datetime,
        end_utc: datetime,
        extra: dict[str, str] | None = None,
    ) -> tuple[tuple[str, dict[str, Any]], ...]:
        normalized = _symbols(symbols)
        _window(start_utc, end_utc)
        params = {
            "symbols": ",".join(normalized),
            "start": start_utc.isoformat(),
            "end": end_utc.isoformat(),
            "feed": "sip",
            "sort": "asc",
            "limit": "10000",
            **(extra or {}),
        }
        rows: list[tuple[str, dict[str, Any]]] = []
        token: str | None = None
        for _ in range(self.max_pages):
            page_params = dict(params)
            if token is not None:
                page_params["page_token"] = token
            payload = self._request(kind, params=page_params)
            raw_grouped = payload.get(kind)
            if not isinstance(raw_grouped, dict):
                raise DirectMarketDataError(
                    f"Alpaca SIP {kind} response contract is invalid"
                )
            grouped = cast(dict[object, object], raw_grouped)
            for raw_symbol, raw_items in grouped.items():
                symbol = str(raw_symbol).strip().upper()
                if symbol not in normalized or not isinstance(raw_items, list):
                    raise DirectMarketDataError(
                        f"Alpaca SIP {kind} response contract is invalid"
                    )
                for raw_item in raw_items:
                    if not isinstance(raw_item, dict):
                        raise DirectMarketDataError(
                            f"Alpaca SIP {kind} response row is invalid"
                        )
                    rows.append(
                        (symbol, cast(dict[str, Any], raw_item))
                    )
            next_token = payload.get("next_page_token")
            if next_token is None:
                return tuple(rows)
            if not isinstance(next_token, str) or not next_token.strip():
                raise DirectMarketDataError(
                    f"Alpaca SIP {kind} pagination token is invalid"
                )
            token = next_token
        raise DirectMarketDataError(
            f"Alpaca SIP {kind} pagination exceeded the safety limit"
        )

    def _request(
        self,
        kind: EndpointKind,
        *,
        params: dict[str, str],
    ) -> dict[str, Any]:
        return self._request_path(
            f"/v2/stocks/{kind}",
            label=f"SIP {kind}",
            params=params,
        )

    def _request_path(
        self,
        path: str,
        *,
        label: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        try:
            response = self._client.get(
                f"{self.base_url}{path}",
                headers=self._headers,
                params=params,
            )
        except (httpx.HTTPError, OSError) as exc:
            raise DirectMarketDataError(
                f"Alpaca {label} request failed: {type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            raise DirectMarketDataError(
                f"Alpaca {label} request failed with HTTP "
                f"{response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise DirectMarketDataError(
                f"Alpaca {label} response was not valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise DirectMarketDataError(
                f"Alpaca {label} response contract is invalid"
            )
        return cast(dict[str, Any], payload)


def _symbols(symbols: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(sorted({symbol.strip().upper() for symbol in symbols}))
    if not normalized or any(
        not SYMBOL_PATTERN.fullmatch(symbol) for symbol in normalized
    ):
        raise ValueError("direct Alpaca symbols are invalid")
    return normalized


def _window(start_utc: datetime, end_utc: datetime) -> None:
    for name, value in (("start_utc", start_utc), ("end_utc", end_utc)):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError(f"{name} must be timezone-aware UTC")
    if end_utc <= start_utc:
        raise ValueError("end_utc must be after start_utc")


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Alpaca SIP event timestamp is missing")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _window_timestamp(value: datetime, *, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("required Alpaca text is missing")
    return value.strip()


def _optional_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
