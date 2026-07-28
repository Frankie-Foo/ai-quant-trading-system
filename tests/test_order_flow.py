from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import polars as pl
import pytest

from data_plane.providers.alpaca import fetch_trades
from kernel.features.order_flow import order_flow_features

ASOF = datetime(2026, 7, 28, 14, 1, tzinfo=UTC)


def _trades() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["FAST"] * 5,
            "ts_utc": [
                ASOF - timedelta(seconds=50),
                ASOF - timedelta(seconds=40),
                ASOF - timedelta(seconds=30),
                ASOF - timedelta(seconds=20),
                ASOF - timedelta(seconds=10),
            ],
            "trade_id": [1, 2, 3, 4, 5],
            "exchange": ["Q"] * 5,
            "price": [100.00, 100.01, 100.01, 99.99, 100.00],
            "size": [100, 200, 50, 150, 100],
            "conditions": [["@"]] * 5,
            "tape": ["C"] * 5,
            "source": ["cloud.alpaca.market_data"] * 5,
            "feed": ["sip"] * 5,
        }
    ).with_columns(pl.col("ts_utc").cast(pl.Datetime("ns", "UTC")))


def _quotes() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["FAST"],
            "ts_utc": [ASOF - timedelta(seconds=1)],
            "bid_price": [99.99],
            "ask_price": [100.01],
            "bid_size": [600.0],
            "ask_size": [400.0],
            "bid_exchange": ["Q"],
            "ask_exchange": ["P"],
            "conditions": [["R"]],
            "tape": ["C"],
            "source": ["cloud.alpaca.market_data"],
            "feed": ["sip"],
        }
    ).with_columns(pl.col("ts_utc").cast(pl.Datetime("ns", "UTC")))


def test_tick_rule_order_flow_matches_the_paper_worked_example() -> None:
    result = order_flow_features(
        _trades(),
        _quotes(),
        symbols=("FAST", "EMPTY"),
        asof_utc=ASOF,
        window=timedelta(minutes=1),
        provenance="test.sip",
    )
    rows = {row["symbol"]: row for row in result.iter_rows(named=True)}
    fast = rows["FAST"]

    assert fast["availability"] == "available"
    assert fast["total_volume"] == 600
    assert fast["classified_volume"] == 500
    assert fast["buy_volume"] == 350
    assert fast["sell_volume"] == 150
    assert fast["order_imbalance"] == pytest.approx(0.4)
    assert fast["buy_sell_pressure_ratio"] == pytest.approx(350 / 150)
    assert fast["vpoc"] == pytest.approx(100.01)
    assert fast["quote_size_imbalance"] == pytest.approx(0.2)
    assert fast["microprice"] == pytest.approx(100.002)
    assert fast["spread_bps"] == pytest.approx(2.0)
    assert rows["EMPTY"]["availability"] == "no_trades"
    assert rows["EMPTY"]["order_imbalance"] is None


def test_future_trade_and_quote_cannot_change_order_flow_at_asof() -> None:
    baseline = order_flow_features(
        _trades(),
        _quotes(),
        symbols=("FAST",),
        asof_utc=ASOF,
        window=timedelta(minutes=1),
        provenance="test.sip",
    )
    future_trades = pl.concat(
        [
            _trades(),
            _trades()
            .head(1)
            .with_columns(
                pl.lit(ASOF + timedelta(microseconds=1))
                    .cast(pl.Datetime("ns", "UTC"))
                    .alias("ts_utc"),
                pl.lit(999, dtype=pl.Int64).alias("trade_id"),
                pl.lit(1_000_000, dtype=pl.Int64).alias("size"),
                pl.lit(999.0).alias("price"),
            ),
        ]
    )
    future_quotes = pl.concat(
        [
            _quotes(),
            _quotes().with_columns(
                pl.lit(ASOF + timedelta(microseconds=1))
                .cast(pl.Datetime("ns", "UTC"))
                .alias("ts_utc"),
                pl.lit(999.0).alias("ask_price"),
            ),
        ]
    )
    mutated = order_flow_features(
        future_trades,
        future_quotes,
        symbols=("FAST",),
        asof_utc=ASOF,
        window=timedelta(minutes=1),
        provenance="test.sip",
    )

    assert mutated.to_dicts() == baseline.to_dicts()


def test_cloud_trade_client_preserves_nanosecond_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUD_PLATFORM_BASE_URL", "http://localhost:8765")
    monkeypatch.setenv("CLOUD_MARKET_DATA_API_TOKEN", "market-token")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/market-data/trades"
        return httpx.Response(
            200,
            json={
                "api_version": "v1",
                "trades": [
                    {
                        "symbol": "FAST",
                        "ts_utc": "2026-07-28T14:00:00.123456789Z",
                        "trade_id": 101,
                        "exchange": "Q",
                        "price": 100.0,
                        "size": 300,
                        "conditions": ["@"],
                        "tape": "C",
                        "source": "cloud.alpaca.market_data",
                        "feed": "sip",
                    }
                ],
                "coverage": {
                    "status": "observed",
                    "fallback_recommended": False,
                    "symbols": [{"symbol": "FAST", "status": "observed"}],
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_trades(
            ("FAST",),
            ASOF - timedelta(minutes=1),
            ASOF,
            client=client,
        )

    assert result.height == 1
    assert result.schema["ts_utc"] == pl.Datetime("ns", "UTC")
    assert result.get_column("trade_id").to_list() == [101]
