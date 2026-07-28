from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from execution.alpaca_sip_stream import (
    PlatformSipStream,
    SipBar,
    SipEvent,
    SipProtocolError,
    SipQuote,
    SipTrade,
    parse_market_data_frame,
)
from scripts.run_paper_session import _poll_next_event
from scripts.stream_alpaca_sip import _lease_window


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
                {
                    "T": "t",
                    "S": "AAPL",
                    "i": 101,
                    "x": "Q",
                    "p": 225.0,
                    "s": 300,
                    "c": ["@"],
                    "z": "C",
                    "t": "2026-07-21T14:37:01.223456Z",
                },
            ]
        )
    )
    assert isinstance(events[0], SipQuote)
    assert isinstance(events[1], SipBar)
    assert isinstance(events[2], SipTrade)
    assert events[2].trade_id == 101
    assert events[0].ts_utc == datetime(2026, 7, 21, 14, 37, 1, 123456, tzinfo=UTC)


def test_platform_stream_uses_scoped_token_and_yields_validated_events() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer market-token"
        assert request.url.path == "/v1/market-data/stream"
        assert request.url.params["after"] == "0"
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
            headers={"Content-Type": "text/event-stream"},
            text=(
                "retry: 1000\n\n"
                "id: 1\n"
                "event: market-data\n"
                f"data: {json.dumps({'sequence': 1, 'event': event})}\n\n"
            ),
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


def test_platform_stream_falls_back_to_cursor_polling_for_an_old_server() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/stream"):
            return httpx.Response(404)
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
                "events": [{"sequence": 1, "event": event}],
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
    assert calls == [
        "/v1/market-data/stream",
        "/v1/market-data/events",
    ]


def test_platform_stream_leases_symbols_before_consuming_from_returned_cursor() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer market-token"
        if request.method == "POST":
            payload = json.loads(request.content)
            assert payload["symbols"] == ["MSFT", "AAPL"]
            return httpx.Response(
                200,
                json={
                    "api_version": "v1",
                    "symbols": ["AAPL", "MSFT"],
                    "expires_at_utc": "2026-07-21T20:05:00Z",
                    "start_after_sequence": 41,
                },
            )
        assert request.url.path == "/v1/market-data/stream"
        assert request.url.params["after"] == "41"
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
            headers={"Content-Type": "text/event-stream"},
            text=(
                "id: 42\n"
                "event: market-data\n"
                f"data: {json.dumps({'sequence': 42, 'event': event})}\n\n"
            ),
        )

    async def run() -> SipBar:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            stream = PlatformSipStream(
                base_url="http://localhost:8765",
                token=SecretStr("market-token"),
                symbols=("MSFT", "aapl"),
                client=client,
            )
            await stream.ensure_subscription(
                replay_from_utc=datetime(2026, 7, 21, 13, 30, tzinfo=UTC),
                expires_at_utc=datetime(2026, 7, 21, 20, 5, tzinfo=UTC),
            )
            events = stream.events()
            event = await anext(events)
            await events.aclose()
            assert isinstance(event, SipBar)
            return event

    assert asyncio.run(run()).symbol == "AAPL"
    assert calls == [
        ("POST", "/v1/market-data/subscriptions"),
        ("GET", "/v1/market-data/stream"),
    ]


def test_platform_health_snapshot_fails_closed_when_fallback_is_recommended() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/market-data/status"
        return httpx.Response(
            200,
            json={
                "api_version": "v1",
                "status": "degraded",
                "fallback_recommended": True,
                "symbols": [
                    {
                        "symbol": "SMCI",
                        "subscribed": True,
                        "status": "stale",
                        "reason_codes": ["event_delay_above_300_seconds"],
                    }
                ],
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            stream = PlatformSipStream(
                base_url="http://localhost:8765",
                token=SecretStr("market-token"),
                symbols=("SMCI",),
                client=client,
            )
            with pytest.raises(SipProtocolError, match="health is not usable"):
                await stream.require_healthy()

    asyncio.run(run())


def test_standalone_stream_lease_window_is_bounded_and_replayable() -> None:
    now = datetime(2026, 7, 21, 13, 30, tzinfo=UTC)
    replay, expiry = _lease_window(now_utc=now, max_seconds=0)
    assert replay == now.replace(minute=25)
    assert expiry == now.replace(hour=1, day=22)
    assert expiry - replay < timedelta(days=2)


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


def test_paper_poll_timeout_does_not_cancel_the_pending_market_event() -> None:
    event = SipBar(
        symbol="AAPL",
        ts_utc=datetime(2026, 7, 21, 14, 36, tzinfo=UTC),
        open=224.0,
        high=225.1,
        low=223.9,
        close=225.0,
        volume=1000,
        trade_count=50,
        vwap=224.7,
        provenance="cloud.alpaca.sip@test",
    )

    async def run() -> SipBar:
        async def delayed_events() -> AsyncGenerator[SipEvent, None]:
            await asyncio.sleep(0.03)
            yield event

        events = delayed_events()
        pending, first = await _poll_next_event(events, pending=None, timeout=0.005)
        assert first is None
        assert pending is not None
        pending, second = await _poll_next_event(events, pending=pending, timeout=0.1)
        assert pending is None
        assert second is event
        await events.aclose()
        return second

    assert asyncio.run(run()) is event
