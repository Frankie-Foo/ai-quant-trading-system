"""Centralized Alpaca SIP WebSocket protocol and event contracts."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

SIP_STREAM_URL = "wss://stream.data.alpaca.markets/v2/sip"
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]*$")


class SipProtocolError(RuntimeError):
    """A sanitized fail-closed protocol or entitlement error."""


class WebSocketLike(Protocol):
    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SipQuote(FrozenModel):
    event_type: str = "quote"
    symbol: str
    ts_utc: datetime
    bid_price: float = Field(ge=0)
    bid_size: int = Field(ge=0)
    ask_price: float = Field(ge=0)
    ask_size: int = Field(ge=0)
    feed: str = "sip"
    provenance: str

    @field_validator("ts_utc")
    @classmethod
    def utc_only(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("SIP event timestamps must be UTC")
        return value


class SipBar(FrozenModel):
    event_type: str = "bar"
    symbol: str
    ts_utc: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0)
    trade_count: int = Field(ge=0)
    vwap: float = Field(gt=0)
    feed: str = "sip"
    provenance: str

    @field_validator("ts_utc")
    @classmethod
    def utc_only(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("SIP event timestamps must be UTC")
        return value


SipEvent = SipQuote | SipBar


class SipSubscription(FrozenModel):
    connected: bool
    authenticated: bool
    bars: tuple[str, ...]
    quotes: tuple[str, ...]


def _decode_frame(frame: str | bytes) -> list[object]:
    try:
        text = frame.decode("utf-8") if isinstance(frame, bytes) else frame
        value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SipProtocolError("invalid JSON frame from SIP stream") from exc
    if not isinstance(value, list):
        raise SipProtocolError("SIP stream frame must be a JSON array")
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise SipProtocolError("SIP event is missing its timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SipProtocolError("SIP event has an invalid timestamp") from exc
    return parsed


def parse_market_data_frame(frame: str | bytes) -> tuple[SipEvent, ...]:
    events: list[SipEvent] = []
    for item in _decode_frame(frame):
        if not isinstance(item, dict):
            raise SipProtocolError("SIP stream message must be an object")
        event_type = item.get("T")
        if event_type not in {"q", "b"}:
            continue
        symbol = str(item.get("S", "")).strip().upper()
        if not SYMBOL_PATTERN.fullmatch(symbol):
            raise SipProtocolError("SIP event has an invalid symbol")
        ts_utc = _timestamp(item.get("t"))
        provenance = f"alpaca.sip.websocket@{ts_utc.isoformat()}"
        try:
            if event_type == "q":
                events.append(
                    SipQuote.model_validate(
                        {
                            "symbol": symbol,
                            "ts_utc": ts_utc,
                            "bid_price": item.get("bp"),
                            "bid_size": item.get("bs"),
                            "ask_price": item.get("ap"),
                            "ask_size": item.get("as"),
                            "provenance": provenance,
                        }
                    )
                )
            else:
                events.append(
                    SipBar.model_validate(
                        {
                            "symbol": symbol,
                            "ts_utc": ts_utc,
                            "open": item.get("o"),
                            "high": item.get("h"),
                            "low": item.get("l"),
                            "close": item.get("c"),
                            "volume": item.get("v"),
                            "trade_count": item.get("n"),
                            "vwap": item.get("vw"),
                            "provenance": provenance,
                        }
                    )
                )
        except Exception as exc:
            raise SipProtocolError("SIP event failed schema validation") from exc
    return tuple(events)


def _contains(messages: list[object], *, event_type: str, message: str | None = None) -> bool:
    return any(
        isinstance(item, dict)
        and item.get("T") == event_type
        and (message is None or item.get("msg") == message)
        for item in messages
    )


class AlpacaSipStream:
    def __init__(self, *, api_key: str, api_secret: str, symbols: tuple[str, ...]):
        if not api_key.strip() or not api_secret.strip():
            raise ValueError("Alpaca credentials are required")
        normalized = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols))
        if not normalized or any(not SYMBOL_PATTERN.fullmatch(symbol) for symbol in normalized):
            raise ValueError("at least one valid stock symbol is required")
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.symbols = normalized
        self.url = SIP_STREAM_URL

    async def authenticate_and_subscribe(self, socket: WebSocketLike) -> SipSubscription:
        welcome = _decode_frame(await socket.recv())
        if not _contains(welcome, event_type="success", message="connected"):
            raise SipProtocolError("SIP stream did not acknowledge connection")
        await socket.send(
            json.dumps(
                {"action": "auth", "key": self.api_key, "secret": self.api_secret},
                separators=(",", ":"),
            )
        )
        authentication = _decode_frame(await socket.recv())
        if not _contains(authentication, event_type="success", message="authenticated"):
            raise SipProtocolError("SIP authentication or entitlement failed")
        await socket.send(
            json.dumps(
                {"action": "subscribe", "bars": list(self.symbols), "quotes": list(self.symbols)},
                separators=(",", ":"),
            )
        )
        acknowledgement = _decode_frame(await socket.recv())
        subscription = next(
            (
                item
                for item in acknowledgement
                if isinstance(item, dict) and item.get("T") == "subscription"
            ),
            None,
        )
        if not isinstance(subscription, dict):
            raise SipProtocolError("SIP stream did not acknowledge subscription")
        bars = tuple(str(item) for item in subscription.get("bars", []))
        quotes = tuple(str(item) for item in subscription.get("quotes", []))
        if not set(self.symbols).issubset(bars) or not set(self.symbols).issubset(quotes):
            raise SipProtocolError("SIP stream returned an incomplete subscription")
        return SipSubscription(
            connected=True,
            authenticated=True,
            bars=bars,
            quotes=quotes,
        )

    async def probe(self, *, timeout_seconds: float = 20.0) -> SipSubscription:
        from websockets.asyncio.client import connect

        async with asyncio.timeout(timeout_seconds):
            async with connect(
                self.url,
                open_timeout=min(timeout_seconds, 10.0),
                ping_interval=20.0,
                ping_timeout=20.0,
                close_timeout=5.0,
                max_size=1_048_576,
                max_queue=16,
            ) as socket:
                return await self.authenticate_and_subscribe(cast(WebSocketLike, socket))

    async def events(self) -> AsyncGenerator[SipEvent, None]:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed

        async for socket in connect(
            self.url,
            open_timeout=10.0,
            ping_interval=20.0,
            ping_timeout=20.0,
            close_timeout=5.0,
            max_size=1_048_576,
            max_queue=16,
        ):
            await self.authenticate_and_subscribe(cast(WebSocketLike, socket))
            try:
                async for frame in socket:
                    for event in parse_market_data_frame(frame):
                        yield event
            except ConnectionClosed:
                continue
