from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from data_plane.providers.perpetual_sentiment import (
    AevoPerpClient,
    HyperliquidPerpClient,
    PerpInstrumentRequest,
    PerpProviderError,
)

ASOF = datetime(2026, 7, 30, 1, 30, tzinfo=UTC)


def _hyperliquid_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> HyperliquidPerpClient:
    return HyperliquidPerpClient(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: ASOF,
    )


def test_hyperliquid_normalizes_public_perpetual_context() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "https://api.hyperliquid.xyz/info"
        assert request.headers.get("authorization") is None
        body = request.read().decode("utf-8")
        if body == '{"type":"metaAndAssetCtxs"}':
            return httpx.Response(
                200,
                json=[
                    {"universe": [{"name": "BTC", "szDecimals": 5}]},
                    [
                        {
                            "dayNtlVlm": "1200000000",
                            "funding": "0.00005",
                            "impactPxs": ["101.9", "102.1"],
                            "markPx": "102",
                            "openInterest": "1100",
                            "oraclePx": "101.95",
                            "premium": "0.00049",
                            "prevDayPx": "100",
                        }
                    ],
                ],
            )
        if body == '{"type":"l2Book","coin":"BTC"}':
            return httpx.Response(
                200,
                json={
                    "coin": "BTC",
                    "time": int(ASOF.timestamp() * 1_000),
                    "levels": [
                        [{"px": "101.9", "sz": "8", "n": 2}],
                        [{"px": "102.1", "sz": "7", "n": 2}],
                    ],
                },
            )
        assert body == '{"type":"recentTrades","coin":"BTC"}'
        return httpx.Response(
            200,
            json=[
                {
                    "coin": "BTC",
                    "side": "B",
                    "px": "102",
                    "sz": "2",
                    "time": int(ASOF.timestamp() * 1_000) - 2_000,
                    "tid": 1,
                },
                {
                    "coin": "BTC",
                    "side": "A",
                    "px": "101.9",
                    "sz": "1",
                    "time": int(ASOF.timestamp() * 1_000) - 1_000,
                    "tid": 2,
                },
                [
                    {
                        "coin": "BTC",
                        "side": "B",
                        "px": "102.1",
                        "sz": "1",
                        "time": int(ASOF.timestamp() * 1_000),
                        "tid": 3,
                    }
                ][0],
            ],
        )

    observations = _hyperliquid_client(handler).fetch(
        (PerpInstrumentRequest(market="main", instrument="BTC"),),
    )

    assert len(observations) == 1
    value = observations[0]
    assert value.key == ("hyperliquid", "main", "btc")
    assert value.mark_price == 102
    assert value.reference_price == 100
    assert value.open_interest == 1100
    assert value.notional_volume_24h == 1_200_000_000
    assert value.funding_rate == 0.00005
    assert value.bid_price == 101.9
    assert value.ask_price == 102.1
    assert value.aggressor_imbalance == pytest.approx(0.50049, abs=1e-5)
    assert value.aggressor_trade_count == 3
    assert value.active is True
    assert value.observed_at_utc == ASOF
    assert "metaAndAssetCtxs+l2Book+recentTrades" in value.provenance


def test_aevo_preserves_missing_open_interest_instead_of_estimating_it() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.method == "GET"
        assert request.headers.get("aevo-key") is None
        assert request.headers.get("aevo-secret") is None
        if request.url.path == "/markets":
            assert request.url.params["asset"] == "BTC"
            return httpx.Response(
                200,
                json=[
                    {
                        "trade_id": "1",
                        "instrument_name": "BTC-PERP",
                        "instrument_type": "PERPETUAL",
                        "underlying_asset": "BTC",
                        "mark_price": "101.5",
                        "index_price": "101.0",
                        "is_active": True,
                        "market_type": "PERPETUAL",
                    }
                ],
            )
        if request.url.path == "/funding":
            assert request.url.params["instrument_name"] == "BTC-PERP"
            return httpx.Response(
                200,
                json={
                    "funding_rate": "0.00004",
                    "next_epoch": "1785376800000000000",
                },
            )
        if request.url.path == "/orderbook":
            assert request.url.params["instrument_name"] == "BTC-PERP"
            return httpx.Response(
                200,
                json={
                    "instrument_name": "BTC-PERP",
                    "instrument_type": "PERPETUAL",
                    "bids": [["101.4", "10"], ["101.3", "20"]],
                    "asks": [["101.6", "8"], ["101.7", "20"]],
                    "last_updated": str(int(ASOF.timestamp() * 1_000_000_000)),
                },
            )
        assert request.url.path == "/instrument/BTC-PERP/trade-history"
        return httpx.Response(
            200,
            json={
                "count": "3",
                "trade_history": [
                    {
                        "trade_id": "2",
                        "instrument_name": "BTC-PERP",
                        "side": "buy",
                        "price": "101.5",
                        "amount": "2",
                        "created_timestamp": str(
                            int(ASOF.timestamp() * 1_000_000_000) - 2_000_000_000
                        ),
                    },
                    {
                        "trade_id": "3",
                        "instrument_name": "BTC-PERP",
                        "side": "sell",
                        "price": "101.4",
                        "amount": "1",
                        "created_timestamp": str(
                            int(ASOF.timestamp() * 1_000_000_000) - 1_000_000_000
                        ),
                    },
                    {
                        "trade_id": "4",
                        "instrument_name": "BTC-PERP",
                        "side": "buy",
                        "price": "101.6",
                        "amount": "1",
                        "created_timestamp": str(
                            int(ASOF.timestamp() * 1_000_000_000)
                        ),
                    },
                ],
            },
        )

    client = AevoPerpClient(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: ASOF,
    )
    observations = client.fetch(
        (PerpInstrumentRequest(market="mainnet", instrument="BTC-PERP"),),
    )

    assert calls == [
        "/markets",
        "/funding",
        "/orderbook",
        "/instrument/BTC-PERP/trade-history",
    ]
    assert len(observations) == 1
    value = observations[0]
    assert value.key == ("aevo", "mainnet", "btc-perp")
    assert value.mark_price == 101.5
    assert value.oracle_price == 101.0
    assert value.funding_rate == 0.00004
    assert value.open_interest is None
    assert value.notional_volume_24h is None
    assert value.bid_price == 101.4
    assert value.ask_price == 101.6
    assert value.aggressor_imbalance == pytest.approx(0.500493, abs=1e-6)
    assert value.aggressor_trade_count == 3
    assert value.active is True
    assert value.observed_at_utc == ASOF
    assert "aevo.rest.markets+funding+orderbook+trade-history" in value.provenance


def test_hyperliquid_delisted_instrument_is_not_active() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8")
        if body == '{"type":"metaAndAssetCtxs"}':
            return httpx.Response(
                200,
                json=[
                    {
                        "universe": [
                            {"name": "BTC", "szDecimals": 5, "isDelisted": True}
                        ]
                    },
                    [
                        {
                            "markPx": "102",
                            "oraclePx": "101.95",
                            "prevDayPx": "100",
                        }
                    ],
                ],
            )
        if '"type":"l2Book"' in body:
            return httpx.Response(
                200,
                json={"coin": "BTC", "levels": [[], []], "time": 1},
            )
        return httpx.Response(200, json=[])

    value = _hyperliquid_client(handler).fetch(
        (PerpInstrumentRequest(market="main", instrument="BTC"),)
    )[0]

    assert value.active is False


def test_hyperliquid_hip3_dex_is_explicit_and_errors_are_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.read().decode("utf-8") == (
            '{"type":"metaAndAssetCtxs","dex":"equities"}'
        )
        return httpx.Response(
            503,
            text="upstream detail that must not escape",
        )

    client = _hyperliquid_client(handler)
    with pytest.raises(PerpProviderError) as captured:
        client.fetch(
            (
                PerpInstrumentRequest(
                    market="equities",
                    instrument="SPACEX",
                ),
            ),
        )

    assert "upstream detail" not in str(captured.value)
    assert "503" not in str(captured.value)
