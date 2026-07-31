from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Self

from pydantic import SecretStr

from data_plane.providers.alpaca_proxy import (
    ALPACA_PROXY_SIP_URL,
    AlpacaProxySipStream,
    probe_alpaca_proxy_sip,
)
from execution.alpaca_sip_stream import SipBar


class _FakeWebSocket:
    def __init__(self, frames: list[str]) -> None:
        self._frames: AsyncIterator[str] = self._iterate(frames)
        self.sent: list[dict[str, object]] = []

    @staticmethod
    async def _iterate(frames: list[str]) -> AsyncIterator[str]:
        for frame in frames:
            yield frame

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def recv(self) -> str:
        return await anext(self._frames)

    async def send(self, message: str) -> None:
        payload = json.loads(message)
        assert isinstance(payload, dict)
        self.sent.append(payload)


def test_proxy_probe_uses_fixed_sip_endpoint_and_redacts_credentials() -> None:
    websocket = _FakeWebSocket(
        [
            '[{"T":"success","msg":"connected"}]',
            '[{"T":"success","msg":"authenticated"}]',
            '[{"T":"subscription","trades":["AAPL"],'
            '"quotes":["AAPL"],"bars":["AAPL"]}]',
        ]
    )

    result = asyncio.run(
        probe_alpaca_proxy_sip(
            key_id=SecretStr("market-key"),
            secret_key=SecretStr("market-secret"),
            connector=lambda *_args, **_kwargs: websocket,
        )
    )

    assert ALPACA_PROXY_SIP_URL == "wss://alpaca-trade-api.vertu.cn/v2/sip"
    assert result == {
        "schema_version": "alpaca_proxy_sip_probe.v1",
        "healthy": True,
        "reason": None,
        "endpoint_host": "alpaca-trade-api.vertu.cn",
        "capabilities": ["bars", "quotes", "trades"],
    }
    assert websocket.sent == [
        {"action": "auth", "key": "market-key", "secret": "market-secret"},
        {
            "action": "subscribe",
            "bars": ["AAPL"],
            "quotes": ["AAPL"],
            "trades": ["AAPL"],
        },
    ]
    serialized = json.dumps(result)
    assert "market-key" not in serialized
    assert "market-secret" not in serialized


def test_proxy_probe_fails_closed_on_rejected_authentication() -> None:
    websocket = _FakeWebSocket(
        [
            '[{"T":"success","msg":"connected"}]',
            '[{"T":"error","code":401,"msg":"auth failed"}]',
        ]
    )

    result = asyncio.run(
        probe_alpaca_proxy_sip(
            key_id=SecretStr("bad-key"),
            secret_key=SecretStr("bad-secret"),
            connector=lambda *_args, **_kwargs: websocket,
        )
    )

    assert result["healthy"] is False
    assert result["reason"] == "authentication_failed"
    assert "bad-key" not in json.dumps(result)
    assert "bad-secret" not in json.dumps(result)


def test_proxy_stream_yields_typed_realtime_events() -> None:
    websocket = _FakeWebSocket(
        [
            '[{"T":"success","msg":"connected"}]',
            '[{"T":"success","msg":"authenticated"}]',
            '[{"T":"subscription","trades":["AAPL"],'
            '"quotes":["AAPL"],"bars":["AAPL"]}]',
            '[{"T":"b","S":"AAPL","o":224.0,"h":225.1,'
            '"l":223.9,"c":225.0,"v":1000,"n":50,"vw":224.7,'
            '"t":"2026-07-31T14:36:00Z"}]',
        ]
    )

    async def first_event() -> SipBar:
        stream = AlpacaProxySipStream(
            key_id=SecretStr("market-key"),
            secret_key=SecretStr("market-secret"),
            symbols=("AAPL",),
            connector=lambda *_args, **_kwargs: websocket,
        )
        events = stream.events()
        event = await anext(events)
        await events.aclose()
        assert isinstance(event, SipBar)
        return event

    event = asyncio.run(first_event())

    assert event.symbol == "AAPL"
    assert event.close == 225.0
    assert event.provenance.startswith("alpaca.sip.websocket@")
