from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import polars as pl
import pytest

from data_plane.providers import alpaca
from kernel.quote_costs import latest_nbbo_spread, window_nbbo_spread

NOW = datetime(2026, 7, 20, 13, 36, tzinfo=UTC)


def test_cloud_quotes_preserve_nanoseconds(
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
                "quotes": [
                    {
                        "symbol": "AAPL",
                        "ts_utc": "2026-07-20T13:35:59.123456789Z",
                        "bid_price": 210.0,
                        "ask_price": 210.02,
                        "bid_size": 4,
                        "ask_size": 7,
                        "bid_exchange": "V",
                        "ask_exchange": "Q",
                        "conditions": ["R"],
                        "tape": "C",
                        "source": "cloud.alpaca.market_data",
                        "feed": "sip",
                    }
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = alpaca.fetch_quotes(
        ("AAPL",), NOW - timedelta(minutes=1), NOW + timedelta(seconds=1), client=client
    )

    assert result.height == 1
    assert result.schema["ts_utc"] == pl.Datetime("ns", "UTC")


def test_latest_nbbo_is_causal_and_fails_closed_when_stale() -> None:
    quotes = pl.DataFrame(
        {
            "symbol": ["AAPL", "AAPL", "AAPL"],
            "ts_utc": [
                NOW - timedelta(seconds=31),
                NOW - timedelta(seconds=1),
                NOW + timedelta(milliseconds=1),
            ],
            "bid_price": [209.0, 210.0, 1.0],
            "ask_price": [209.1, 210.02, 999.0],
            "source": ["alpaca.market_data"] * 3,
            "feed": ["sip"] * 3,
        }
    ).with_columns(pl.col("ts_utc").cast(pl.Datetime("ns", "UTC")))

    observed = latest_nbbo_spread(quotes, symbol="AAPL", at_utc=NOW)
    assert observed is not None
    assert observed.quote_ts_utc == NOW - timedelta(seconds=1)
    assert observed.relative_spread == pytest.approx(0.02 / 210.01)
    assert observed.age_seconds == pytest.approx(1.0)
    assert "nbbo" in observed.provenance

    assert (
        latest_nbbo_spread(
            quotes,
            symbol="AAPL",
            at_utc=NOW + timedelta(minutes=1),
            max_age=timedelta(seconds=10),
        )
        is None
    )


def test_window_nbbo_uses_conservative_observed_quantile() -> None:
    quotes = pl.DataFrame(
        {
            "symbol": ["AAPL"] * 3,
            "ts_utc": [
                NOW + timedelta(seconds=1),
                NOW + timedelta(seconds=2),
                NOW + timedelta(seconds=3),
            ],
            "bid_price": [100.0, 100.0, 100.0],
            "ask_price": [100.01, 100.02, 100.10],
            "source": ["alpaca.market_data"] * 3,
            "feed": ["sip"] * 3,
        }
    ).with_columns(pl.col("ts_utc").cast(pl.Datetime("ns", "UTC")))
    observed = window_nbbo_spread(
        quotes,
        symbol="AAPL",
        start_utc=NOW,
        end_utc=NOW + timedelta(minutes=1),
    )
    assert observed is not None
    assert observed.sample_count == 3
    assert observed.relative_spread == pytest.approx(0.1 / 100.05)
    assert "quantile=0.95" in observed.provenance
