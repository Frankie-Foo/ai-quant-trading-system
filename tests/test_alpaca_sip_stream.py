from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from execution.alpaca_sip_stream import (
    AlpacaSipStream,
    SipBar,
    SipProtocolError,
    SipQuote,
    parse_market_data_frame,
)


class FakeSocket:
    def __init__(self, incoming: list[str]):
        self.incoming = incoming
        self.sent: list[dict[str, object]] = []

    async def recv(self) -> str:
        return self.incoming.pop(0)

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))


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
    assert events[1].provenance.startswith("alpaca.sip.websocket")


def test_authenticate_and_subscribe_uses_sip_and_requested_symbols() -> None:
    socket = FakeSocket(
        [
            '[{"T":"success","msg":"connected"}]',
            '[{"T":"success","msg":"authenticated"}]',
            '[{"T":"subscription","trades":[],"quotes":["AAPL"],"bars":["AAPL"]}]',
        ]
    )
    stream = AlpacaSipStream(api_key="key", api_secret="secret", symbols=("aapl",))

    result = asyncio.run(stream.authenticate_and_subscribe(socket))

    assert stream.url == "wss://stream.data.alpaca.markets/v2/sip"
    assert socket.sent[0] == {"action": "auth", "key": "key", "secret": "secret"}
    assert socket.sent[1] == {"action": "subscribe", "bars": ["AAPL"], "quotes": ["AAPL"]}
    assert result.authenticated is True
    assert result.bars == ("AAPL",)
    assert result.quotes == ("AAPL",)


def test_authentication_error_fails_closed_without_subscribing() -> None:
    socket = FakeSocket(
        [
            '[{"T":"success","msg":"connected"}]',
            '[{"T":"error","code":402,"msg":"auth failed"}]',
        ]
    )
    stream = AlpacaSipStream(api_key="key", api_secret="secret", symbols=("AAPL",))

    with pytest.raises(SipProtocolError, match="authentication"):
        asyncio.run(stream.authenticate_and_subscribe(socket))
    assert len(socket.sent) == 1
