"""Regression checks for Paper entry quotes on a drifting Windows clock."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from data_plane.providers import alpaca
from data_plane.providers.alpaca_direct import (
    DirectAlpacaMarketDataClient,
    DirectMarketDataError,
)
from execution.alpaca_paper import FreshNbboQuote, build_protected_entry
from scripts import monitor_modern_momentum_paper as paper


def _client(handler):
    return DirectAlpacaMarketDataClient(
        key_id=SecretStr("test-key"),
        secret_key=SecretStr("test-secret"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_latest_sip_quote_uses_server_clock_not_local_clock(monkeypatch):
    def handler(request):
        assert request.url.path == "/v2/stocks/LITE/quotes/latest"
        assert request.url.params["feed"] == "sip"
        return httpx.Response(
            200,
            headers={"Date": "Mon, 28 Sep 2026 13:30:01 GMT"},
            json={"quote": {"t": "2026-09-28T13:30:01Z", "bp": 100.0,
                            "bs": 10, "ap": 100.02, "as": 10}},
        )

    observation = _client(handler).fetch_latest_quote("LITE")
    assert observation.quote.ask_price == 100.02
    assert observation.observed_at_utc() >= datetime(2026, 9, 28, 13, 30, 1, tzinfo=UTC)


def test_latest_sip_quote_rejects_missing_server_clock():
    def handler(request):
        return httpx.Response(
            200,
            json={"quote": {"t": "2026-09-28T13:30:01Z", "bp": 100.0,
                            "bs": 10, "ap": 100.02, "as": 10}},
        )

    with pytest.raises(DirectMarketDataError, match="server clock"):
        _client(handler).fetch_latest_quote("LITE")


def test_paper_quote_path_calls_latest_endpoint(monkeypatch):
    quote = SimpleNamespace(
        symbol="LITE", bid_price=100.0, ask_price=100.02,
        ts_utc=datetime(2026, 9, 28, 13, 30, 1, tzinfo=UTC), feed="sip",
    )
    observation = SimpleNamespace(quote=quote, observed_at_utc=lambda: quote.ts_utc)
    monkeypatch.setattr(paper, "latest_sip_quote", lambda symbol: observation)
    actual, clock = paper._latest_sip_nbbo_now("LITE")
    assert actual.symbol == "LITE" and actual.ask == Decimal("100.02")
    assert clock.observed_at_utc() == quote.ts_utc


def test_slow_response_retries_one_latest_quote_before_blocking(monkeypatch):
    timestamp = datetime(2026, 9, 28, 13, 30, 1, tzinfo=UTC)
    quote = SimpleNamespace(ts_utc=timestamp)
    first = SimpleNamespace(
        quote=quote, request_duration_seconds=2.0,
        observed_at_utc=lambda: datetime(2026, 9, 28, 13, 30, 4, tzinfo=UTC),
    )
    second = SimpleNamespace(
        quote=quote, request_duration_seconds=0.1,
        observed_at_utc=lambda: datetime(2026, 9, 28, 13, 30, 2, tzinfo=UTC),
    )

    class FakeClient:
        def __init__(self):
            self.calls = 0
            self.closed = False

        def fetch_latest_quote(self, symbol):
            assert symbol == "LITE"
            self.calls += 1
            return first if self.calls == 1 else second

        def close(self):
            self.closed = True

    client = FakeClient()
    monkeypatch.setattr(alpaca, "_direct_client", lambda feed: client)
    assert alpaca.latest_sip_quote("LITE") is second
    assert client.calls == 2 and client.closed


def test_monotonic_elapsed_time_still_blocks_stale_entry(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            headers={"Date": "Mon, 28 Sep 2026 13:30:01 GMT"},
            json={"quote": {"t": "2026-09-28T13:30:01Z", "bp": 100.0,
                            "bs": 10, "ap": 100.02, "as": 10}},
        )

    observation = _client(handler).fetch_latest_quote("LITE")
    monkeypatch.setattr(
        "data_plane.providers.alpaca_direct.time.monotonic",
        lambda: observation.received_monotonic + 3,
    )
    quote = FreshNbboQuote(
        symbol="LITE", bid=Decimal("100"), ask=Decimal("100.02"),
        asof_utc=observation.quote.ts_utc, feed="sip",
    )
    with pytest.raises(ValueError, match="stale"):
        build_protected_entry(
            client_order_id="test-LITE", symbol="LITE", qty=1,
            signal_reference=Decimal("100.02"), structural_stop=Decimal("99.50"),
            quote=quote, observed_at_utc=observation.observed_at_utc(),
        )
