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
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_hyperliquid_normalizes_public_perpetual_context() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "https://api.hyperliquid.xyz/info"
        assert request.headers.get("authorization") is None
        assert request.read().decode("utf-8") == '{"type":"metaAndAssetCtxs"}'
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

    observations = _hyperliquid_client(handler).fetch(
        (PerpInstrumentRequest(market="main", instrument="BTC"),),
        observed_at_utc=ASOF,
    )

    assert len(observations) == 1
    value = observations[0]
    assert value.key == ("hyperliquid", "main", "btc")
    assert value.mark_price == 102
    assert value.reference_price == 100
    assert value.open_interest == 1100
    assert value.notional_volume_24h == 1_200_000_000
    assert value.funding_rate == 0.00005
    assert value.bid_price is None
    assert value.ask_price is None
    assert value.active is True
    assert "metaAndAssetCtxs" in value.provenance


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
        assert request.url.path == "/funding"
        assert request.url.params["instrument_name"] == "BTC-PERP"
        return httpx.Response(
            200,
            json={"funding_rate": "0.00004", "next_epoch": "1785376800000000000"},
        )

    client = AevoPerpClient(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    observations = client.fetch(
        (PerpInstrumentRequest(market="mainnet", instrument="BTC-PERP"),),
        observed_at_utc=ASOF,
    )

    assert calls == ["/markets", "/funding"]
    assert len(observations) == 1
    value = observations[0]
    assert value.key == ("aevo", "mainnet", "btc-perp")
    assert value.mark_price == 101.5
    assert value.oracle_price == 101.0
    assert value.funding_rate == 0.00004
    assert value.open_interest is None
    assert value.notional_volume_24h is None
    assert value.active is True
    assert "aevo.rest.markets+funding" in value.provenance


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
            observed_at_utc=ASOF,
        )

    assert "upstream detail" not in str(captured.value)
    assert "503" not in str(captured.value)
