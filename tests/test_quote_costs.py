from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from data_plane.providers import alpaca
from kernel.quote_costs import latest_nbbo_spread, window_nbbo_spread

NOW = datetime(2026, 7, 20, 13, 36, tzinfo=UTC)


def test_alpaca_quotes_preserve_nanoseconds_and_paginate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPACA_API_KEY_ID", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "test-secret")
    calls: list[dict[str, object]] = []

    def fake_get_json(
        url: str, *, params: dict[str, object], headers: dict[str, str]
    ) -> dict[str, object]:
        del url, headers
        calls.append(dict(params))
        suffix = "123456789Z" if len(calls) == 1 else "987654321Z"
        payload: dict[str, object] = {
            "quotes": {
                "AAPL": [
                    {
                        "t": f"2026-07-20T13:35:59.{suffix}",
                        "bp": 210.0,
                        "ap": 210.02,
                        "bs": 4,
                        "as": 7,
                        "bx": "V",
                        "ax": "Q",
                        "c": ["R"],
                        "z": "C",
                    }
                ]
            }
        }
        if len(calls) == 1:
            payload["next_page_token"] = "opaque"
        return payload

    monkeypatch.setattr(alpaca, "get_json", fake_get_json)
    result = alpaca.fetch_quotes(
        ("AAPL",), NOW - timedelta(minutes=1), NOW + timedelta(seconds=1)
    )

    assert result.height == 2
    assert result.schema["ts_utc"] == pl.Datetime("ns", "UTC")
    assert calls[1]["page_token"] == "opaque"


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
