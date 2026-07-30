from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
from perp_risk.config import load_config
from perp_risk.providers import AevoClient, HyperliquidClient, JsonTransport

NOW = datetime(2026, 7, 30, 14, 0, tzinfo=UTC)


def test_hyperliquid_parses_hip3_prefix_bbo_and_aggressor_flow() -> None:
    config = load_config()
    binding = next(item for item in config.bindings if item.instrument == "CL")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["type"] == "metaAndAssetCtxs":
            assert payload["dex"] == "xyz"
            return httpx.Response(
                200,
                json=[
                    {"universe": [{"name": "xyz:CL"}]},
                    [
                        {
                            "markPx": "84.0",
                            "oraclePx": "84.0",
                            "prevDayPx": "82.0",
                            "openInterest": "1000",
                            "funding": "0.00001",
                            "dayNtlVlm": "100000000",
                        }
                    ],
                ],
            )
        if payload["type"] == "l2Book":
            assert payload["coin"] == "xyz:CL"
            return httpx.Response(
                200,
                json={
                    "coin": "xyz:CL",
                    "levels": [
                        [{"px": "83.99", "sz": "10"}],
                        [{"px": "84.01", "sz": "12"}],
                    ],
                },
            )
        assert payload["type"] == "recentTrades"
        return httpx.Response(
            200,
            json=[
                {
                    "coin": "xyz:CL",
                    "time": int(NOW.timestamp() * 1000) - 1000,
                    "tid": index,
                    "px": "84",
                    "sz": "1",
                    "side": "B" if index < 3 else "A",
                }
                for index in range(4)
            ],
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = JsonTransport(config=config.http, client=client, sleep=lambda _: None)
    provider = HyperliquidClient(
        collection=config.collection,
        http=config.http,
        transport=transport,
        clock=lambda: NOW,
    )

    result = provider.fetch((binding,))

    assert result.status.status == "ok"
    assert result.observations[0].instrument == "CL"
    assert result.observations[0].bid_price == 83.99
    assert result.observations[0].ask_price == 84.01
    assert result.observations[0].aggressor_trade_count == 4
    assert result.observations[0].aggressor_imbalance == 0.5


def test_hyperliquid_respects_delisted_status() -> None:
    config = load_config()
    binding = next(item for item in config.bindings if item.instrument == "SMH")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["type"] == "metaAndAssetCtxs":
            return httpx.Response(
                200,
                json=[
                    {"universe": [{"name": "xyz:SMH", "isDelisted": True}]},
                    [
                        {
                            "markPx": "500",
                            "oraclePx": "500",
                            "openInterest": "10",
                            "funding": "0",
                            "dayNtlVlm": "2000000",
                        }
                    ],
                ],
            )
        if payload["type"] == "l2Book":
            return httpx.Response(
                200,
                json={
                    "coin": "xyz:SMH",
                    "levels": [
                        [{"px": "499", "sz": "1"}],
                        [{"px": "501", "sz": "1"}],
                    ],
                },
            )
        return httpx.Response(200, json=[])

    provider = HyperliquidClient(
        collection=config.collection,
        http=config.http,
        transport=JsonTransport(
            config=config.http,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda _: None,
        ),
        clock=lambda: NOW,
    )

    assert provider.fetch((binding,)).observations[0].active is False


def test_hyperliquid_keeps_valid_instrument_when_peer_schema_is_invalid() -> None:
    config = load_config()
    bindings = tuple(
        item
        for item in config.bindings
        if item.venue == "hyperliquid" and item.instrument in {"CL", "SMH"}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["type"] == "metaAndAssetCtxs":
            return httpx.Response(
                200,
                json=[
                    {
                        "universe": [
                            {"name": "xyz:CL"},
                            {"name": "xyz:SMH"},
                        ]
                    },
                    [
                        {
                            "markPx": "84",
                            "oraclePx": "84",
                            "openInterest": "100",
                            "funding": "0",
                            "dayNtlVlm": "100000000",
                        },
                        {
                            "markPx": "not-a-number",
                            "oraclePx": "500",
                            "openInterest": "10",
                            "funding": "0",
                            "dayNtlVlm": "2000000",
                        },
                    ],
                ],
            )
        coin = str(payload["coin"])
        if payload["type"] == "l2Book":
            mark = 84 if coin.endswith("CL") else 500
            return httpx.Response(
                200,
                json={
                    "coin": coin,
                    "levels": [
                        [{"px": str(mark - 0.01), "sz": "1"}],
                        [{"px": str(mark + 0.01), "sz": "1"}],
                    ],
                },
            )
        return httpx.Response(200, json=[])

    provider = HyperliquidClient(
        collection=config.collection,
        http=config.http,
        transport=JsonTransport(
            config=config.http,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda _: None,
        ),
        clock=lambda: NOW,
    )

    result = provider.fetch(bindings)

    assert result.status.status == "partial"
    assert [item.instrument for item in result.observations] == ["CL"]
    assert "xyz:SMH:schema_invalid" in result.status.warnings


def test_aevo_parses_orderbook_and_trade_history() -> None:
    config = load_config()
    binding = next(
        item for item in config.bindings if item.venue == "aevo" and item.instrument == "BTC-PERP"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/markets":
            return httpx.Response(
                200,
                json=[
                    {
                        "instrument_name": "BTC-PERP",
                        "mark_price": "64000",
                        "index_price": "63990",
                        "is_active": True,
                    }
                ],
            )
        if request.url.path == "/funding":
            return httpx.Response(200, json={"funding_rate": "0.00001"})
        if request.url.path == "/orderbook":
            return httpx.Response(
                200,
                json={
                    "instrument_name": "BTC-PERP",
                    "bids": [["63999", "1"]],
                    "asks": [["64001", "1"]],
                },
            )
        return httpx.Response(
            200,
            json={
                "trade_history": [
                    {
                        "instrument_name": "BTC-PERP",
                        "created_timestamp": int(NOW.timestamp() * 1_000_000_000) - 1,
                        "trade_id": str(index),
                        "price": "64000",
                        "amount": "1",
                        "side": "buy" if index < 2 else "sell",
                    }
                    for index in range(3)
                ]
            },
        )

    provider = AevoClient(
        collection=config.collection,
        http=config.http,
        transport=JsonTransport(
            config=config.http,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda _: None,
        ),
        clock=lambda: NOW,
    )

    observation = provider.fetch((binding,)).observations[0]

    assert observation.bid_price == 63999
    assert observation.ask_price == 64001
    assert observation.aggressor_trade_count == 3
    assert observation.aggressor_imbalance == 1 / 3
