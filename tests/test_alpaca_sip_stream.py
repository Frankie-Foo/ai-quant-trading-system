from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from execution.alpaca_sip_stream import (
    PlatformSipStream,
    SipBar,
    SipProtocolError,
    SipQuote,
    parse_market_data_frame,
)


def test_parse_sip_quote_and_bar_with_utc_provenance() -> None:
    events = parse_market_data_frame(
        json.dumps(
            [
                {
                    "T": "q",
                    "S": "AAPL",
                    "bp": 224.9,
                    "bs": 2,
                    "ap": 225.0,
                    "as": 4,
                    "t": "2026-07-21T14:37:01.123456Z",
                },
                {
                    "T": "b",
                    "S": "AAPL",
                    "o": 224.0,
                    "h": 225.1,
                    "l": 223.9,
                    "c": 225.0,
                    "v": 1000,
                    "n": 50,
                    "vw": 224.7,
                    "t": "2026-07-21T14:36:00Z",
                },
            ]
        )
    )
    assert isinstance(events[0], SipQuote)
    assert isinstance(events[1], SipBar)
    assert events[0].ts_utc == datetime(2026, 7, 21, 14, 37, 1, 123456, tzinfo=UTC)


def test_platform_stream_uses_scoped_token_and_yields_validated_events() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["Authorization"] == "Bearer market-token"
        event = {
            "event_type": "bar",
            "symbol": "AAPL",
            "ts_utc": "2026-07-21T14:36:00Z",
            "open": 224.0,
            "high": 225.1,
            "low": 223.9,
            "close": 225.0,
            "volume": 1000,
            "trade_count": 50,
            "vwap": 224.7,
            "feed": "sip",
            "provenance": "cloud.alpaca.sip@test",
        }
        return httpx.Response(
            200,
            json={
                "api_version": "v1",
                "events": [{"sequence": 1, "event": event}] if calls == 1 else [],
                "next_sequence": 1,
            },
        )

    async def run() -> SipBar:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            stream = PlatformSipStream(
                base_url="http://localhost:8765",
                token=SecretStr("market-token"),
                symbols=("aapl",),
                client=client,
            )
            events = stream.events()
            event = await anext(events)
            await events.aclose()
            assert isinstance(event, SipBar)
            return event

    assert asyncio.run(run()).symbol == "AAPL"


def test_platform_authentication_error_fails_closed() -> None:
    async def run() -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(401))
        async with httpx.AsyncClient(transport=transport) as client:
            stream = PlatformSipStream(
                base_url="http://127.0.0.1:8765",
                token=SecretStr("bad-token"),
                symbols=("AAPL",),
                client=client,
            )
            with pytest.raises(SipProtocolError):
                await stream.probe()

    asyncio.run(run())
