from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from data_plane.providers.alpaca_direct import (
    DirectAlpacaMarketDataClient,
    DirectMarketDataError,
)

START = datetime(2026, 7, 29, 13, 30, tzinfo=UTC)
END = datetime(2026, 7, 29, 13, 32, tzinfo=UTC)


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> DirectAlpacaMarketDataClient:
    return DirectAlpacaMarketDataClient(
        key_id=SecretStr("market-key"),
        secret_key=SecretStr("market-secret"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_direct_market_data_is_pinned_to_alpaca_data_host() -> None:
    with pytest.raises(ValueError, match="data host"):
        DirectAlpacaMarketDataClient(
            key_id=SecretStr("key"),
            secret_key=SecretStr("secret"),
            base_url="https://example.com",
        )


def test_direct_bars_follow_pagination_and_preserve_sip_provenance() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["APCA-API-KEY-ID"] == "market-key"
        assert request.headers["APCA-API-SECRET-KEY"] == "market-secret"
        assert request.url.path == "/v2/stocks/bars"
        assert request.url.params["symbols"] == "AAPL,MSFT"
        assert request.url.params["feed"] == "sip"
        assert request.url.params["timeframe"] == "1Min"
        assert request.url.params["adjustment"] == "split"
        if calls == 1:
            assert "page_token" not in request.url.params
            return httpx.Response(
                200,
                json={
                    "bars": {
                        "AAPL": [
                            {
                                "t": "2026-07-29T13:30:00Z",
                                "o": 100.0,
                                "h": 101.0,
                                "l": 99.5,
                                "c": 100.5,
                                "v": 1000,
                                "n": 50,
                                "vw": 100.25,
                            }
                        ]
                    },
                    "next_page_token": "next-1",
                },
            )
        assert request.url.params["page_token"] == "next-1"
        return httpx.Response(
            200,
            json={
                "bars": {
                    "MSFT": [
                        {
                            "t": "2026-07-29T13:30:00Z",
                            "o": 200.0,
                            "h": 201.0,
                            "l": 199.5,
                            "c": 200.5,
                            "v": 2000,
                            "n": 75,
                            "vw": 200.25,
                        }
                    ]
                },
                "next_page_token": None,
            },
        )

    bars = _client(handler).fetch_bars(
        ("msft", "AAPL"),
        start_utc=START,
        end_utc=END,
    )

    assert calls == 2
    assert [bar.symbol for bar in bars] == ["AAPL", "MSFT"]
    assert all(bar.feed == "sip" for bar in bars)
    assert all("alpaca.sip.rest.bars" in bar.provenance for bar in bars)


def test_direct_daily_bars_forward_daily_timeframe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["timeframe"] == "1Day"
        assert request.url.params["adjustment"] == "raw"
        return httpx.Response(
            200,
            json={
                "bars": {
                    "AAPL": [
                        {
                            "t": "2026-07-29T04:00:00Z",
                            "o": 100.0,
                            "h": 101.0,
                            "l": 99.5,
                            "c": 100.5,
                            "v": 1000,
                            "n": 50,
                            "vw": 0,
                        }
                    ]
                },
                "next_page_token": None,
            },
        )

    bars = _client(handler).fetch_bars(
        ("AAPL",),
        start_utc=START.replace(hour=0),
        end_utc=END.replace(hour=23),
        timeframe="1Day",
        adjustment="raw",
    )

    assert len(bars) == 1
    assert bars[0].vwap is None


def test_direct_quotes_and_trades_are_schema_validated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/quotes"):
            return httpx.Response(
                200,
                json={
                    "quotes": {
                        "AAPL": [
                            {
                                "t": "2026-07-29T13:31:01Z",
                                "bp": 100.0,
                                "bs": 5,
                                "ap": 100.02,
                                "as": 7,
                            }
                        ]
                    },
                    "next_page_token": None,
                },
            )
        return httpx.Response(
            200,
            json={
                "trades": {
                    "AAPL": [
                        {
                            "t": "2026-07-29T13:31:01.100Z",
                            "i": 42,
                            "x": "Q",
                            "p": 100.02,
                            "s": 100,
                            "c": ["@"],
                            "z": "C",
                        }
                    ]
                },
                "next_page_token": None,
            },
        )

    client = _client(handler)
    quotes = client.fetch_quotes(("AAPL",), start_utc=START, end_utc=END)
    trades = client.fetch_trades(("AAPL",), start_utc=START, end_utc=END)

    assert quotes[0].bid_price == 100.0
    assert quotes[0].ask_price == 100.02
    assert trades[0].trade_id == 42
    assert trades[0].conditions == ("@",)


def test_direct_market_data_errors_are_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="market-secret entitlement detail")

    with pytest.raises(DirectMarketDataError) as captured:
        _client(handler).fetch_quotes(("AAPL",), start_utc=START, end_utc=END)

    assert "market-secret" not in str(captured.value)
    assert "403" in str(captured.value)


def test_direct_market_data_retries_transient_connection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("TLS EOF", request=request)
        return httpx.Response(
            200,
            json={"quotes": {"AAPL": []}, "next_page_token": None},
        )

    monkeypatch.setattr("data_plane.providers.alpaca_direct.time.sleep", lambda _: None)
    quotes = _client(handler).fetch_quotes(
        ("AAPL",), start_utc=START, end_utc=END
    )

    assert quotes == ()
    assert calls == 2


def test_direct_news_is_paginated_bounded_and_symbol_filtered() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/v1beta1/news"
        assert request.url.params["symbols"] == "AAPL"
        assert request.url.params["start"] == START.isoformat()
        assert request.url.params["end"] == END.isoformat()
        assert request.url.params["include_content"] == "false"
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "news": [
                        {
                            "id": 101,
                            "headline": "AAPL launches a product",
                            "summary": "A product launch was announced.",
                            "author": "Desk",
                            "created_at": "2026-07-29T13:30:30Z",
                            "updated_at": "2026-07-29T13:30:31Z",
                            "url": "https://example.com/101",
                            "symbols": ["AAPL"],
                            "source": "benzinga",
                        },
                        {
                            "id": 102,
                            "headline": "AAPL updated after as-of",
                            "summary": "This row is not causal at the requested as-of.",
                            "author": "Desk",
                            "created_at": "2026-07-29T13:30:30Z",
                            "updated_at": "2026-07-29T13:32:01Z",
                            "url": "https://example.com/102",
                            "symbols": ["AAPL"],
                            "source": "benzinga",
                        }
                    ],
                    "next_page_token": "page-2",
                },
            )
        assert request.url.params["page_token"] == "page-2"
        return httpx.Response(
            200,
            json={"news": [], "next_page_token": None},
        )

    articles = _client(handler).fetch_news(
        ("AAPL",),
        start_utc=START,
        end_utc=END,
    )

    assert calls == 2
    assert len(articles) == 1
    assert articles[0].article_id == "101"
    assert articles[0].symbols == ("AAPL",)
    assert articles[0].created_at_utc <= END
    assert articles[0].provenance == "alpaca.news.benzinga:101"
