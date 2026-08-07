from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from data_plane.http import DownloadError
from data_plane.providers.alpaca import (
    fetch_bars,
    fetch_quotes,
    fetch_sparse_bars_for_monitoring,
    fetch_trades,
)
from execution.alpaca_sip_stream import SipBar, SipQuote, SipTrade

START = datetime(2026, 7, 22, 15, 44, tzinfo=UTC)
END = datetime(2026, 7, 22, 15, 46, tzinfo=UTC)


def test_alpaca_direct_is_default_market_data_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MARKET_DATA_PROVIDER", raising=False)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "key-id")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret-key")

    class FakeDirectClient:
        def __init__(self, **_: object) -> None:
            pass

        def fetch_bars(
            self,
            symbols: tuple[str, ...],
            *,
            start_utc: datetime,
            end_utc: datetime,
        ) -> tuple[SipBar, ...]:
            assert symbols == ("AAPL",)
            assert start_utc == START
            assert end_utc == END
            return (
                SipBar(
                    symbol="AAPL",
                    ts_utc=START,
                    open=100,
                    high=101,
                    low=99,
                    close=100.5,
                    volume=10,
                    trade_count=2,
                    vwap=100.25,
                    provenance="alpaca.sip.rest.bars@test",
                ),
            )

        def fetch_quotes(
            self,
            symbols: tuple[str, ...],
            *,
            start_utc: datetime,
            end_utc: datetime,
        ) -> tuple[SipQuote, ...]:
            assert symbols == ("AAPL",)
            assert start_utc == START
            assert end_utc == END
            return (
                SipQuote(
                    symbol="AAPL",
                    ts_utc=START,
                    bid_price=100,
                    bid_size=10,
                    ask_price=100.5,
                    ask_size=12,
                    provenance="alpaca.sip.rest.quotes@test",
                ),
            )

        def fetch_trades(
            self,
            symbols: tuple[str, ...],
            *,
            start_utc: datetime,
            end_utc: datetime,
        ) -> tuple[SipTrade, ...]:
            assert symbols == ("AAPL",)
            assert start_utc == START
            assert end_utc == END
            return (
                SipTrade(
                    symbol="AAPL",
                    ts_utc=START,
                    trade_id=1,
                    exchange="V",
                    price=100.25,
                    size=5,
                    tape="A",
                    provenance="alpaca.sip.rest.trades@test",
                ),
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "data_plane.providers.alpaca.DirectAlpacaMarketDataClient",
        FakeDirectClient,
    )

    frame = fetch_bars(("AAPL",), START, END)

    assert frame.height == 1
    assert frame.get_column("source").to_list() == ["alpaca.sip.rest.bars"]
    assert frame.get_column("feed").to_list() == ["sip"]

    quotes = fetch_quotes(("AAPL",), START, END)
    trades = fetch_trades(("AAPL",), START, END)
    assert quotes.get_column("source").to_list() == ["alpaca.sip.rest.quotes"]
    assert trades.get_column("source").to_list() == ["alpaca.sip.rest.trades"]

    sparse, coverage = fetch_sparse_bars_for_monitoring(("AAPL",), START, END)

    assert sparse.height == 1
    assert coverage["status"] == "observed"
    assert coverage["fallback_recommended"] is False


def test_historical_bars_fail_closed_when_cloud_recommends_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUD_PLATFORM_BASE_URL", "http://localhost:8765")
    monkeypatch.setenv("CLOUD_MARKET_DATA_API_TOKEN", "market-token")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer market-token"
        return httpx.Response(
            200,
            json={
                "api_version": "v1",
                "bars": [],
                "coverage": {
                    "status": "empty",
                    "fallback_recommended": True,
                    "symbols": [
                        {
                            "symbol": "SMCI",
                            "status": "empty",
                            "reason_codes": [
                                "upstream_empty",
                                "regular_session_missing",
                            ],
                        }
                    ],
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DownloadError, match="coverage is not usable"):
            fetch_bars(("SMCI",), START, END, client=client)


def test_historical_bars_accept_explicit_observed_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUD_PLATFORM_BASE_URL", "http://localhost:8765")
    monkeypatch.setenv("CLOUD_MARKET_DATA_API_TOKEN", "market-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "api_version": "v1",
                "bars": [
                    {
                        "symbol": "AAPL",
                        "ts_utc": "2026-07-22T15:44:00Z",
                        "open": 100,
                        "high": 101,
                        "low": 99,
                        "close": 100.5,
                        "volume": 10,
                        "trade_count": 2,
                        "vwap": 100.25,
                        "source": "cloud.alpaca.market_data",
                        "feed": "sip",
                        "adjustment": "split_adjusted",
                    }
                ],
                "coverage": {
                    "status": "observed",
                    "fallback_recommended": False,
                    "symbols": [
                        {
                            "symbol": "AAPL",
                            "status": "observed",
                            "reason_codes": [],
                        }
                    ],
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        frame = fetch_bars(("AAPL",), START, END, client=client)
    assert frame.height == 1
    assert frame.get_column("symbol").to_list() == ["AAPL"]


def test_advisory_monitor_can_preserve_sparse_bars_and_gap_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUD_PLATFORM_BASE_URL", "http://localhost:8765")
    monkeypatch.setenv("CLOUD_MARKET_DATA_API_TOKEN", "market-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "api_version": "v1",
                "bars": [
                    {
                        "symbol": "RNG",
                        "ts_utc": "2026-07-22T15:44:00Z",
                        "open": 47,
                        "high": 48,
                        "low": 46.5,
                        "close": 47.5,
                        "volume": 100,
                        "trade_count": 20,
                        "vwap": 47.25,
                    }
                ],
                "coverage": {
                    "status": "gaps_detected",
                    "fallback_recommended": True,
                    "symbols": [
                        {
                            "symbol": "RNG",
                            "status": "gaps_detected",
                            "missing_minute_count": 1,
                            "reason_codes": ["minute_gaps_detected"],
                        }
                    ],
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        frame, coverage = fetch_sparse_bars_for_monitoring(
            ("RNG",),
            START,
            END,
            client=client,
        )

    assert frame.height == 1
    assert coverage["status"] == "gaps_detected"
    assert coverage["fallback_recommended"] is True
