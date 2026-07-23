"""Keyless realtime event client for the isolated cloud platform API."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

PLATFORM_API_VERSION = "v1"
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]*$")


class SipProtocolError(RuntimeError):
    """A sanitized fail-closed protocol or entitlement error."""


class _LegacyStreamUnsupported(Exception):
    """The server predates the SSE endpoint and requires cursor polling."""


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


class MarketSymbolHealth(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    symbol: str
    subscribed: bool
    status: str
    reason_codes: tuple[str, ...]


class MarketDataHealth(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    api_version: str
    status: str
    fallback_recommended: bool
    symbols: tuple[MarketSymbolHealth, ...]


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


def _event_from_envelope(value: object) -> tuple[int, SipEvent]:
    if not isinstance(value, dict):
        raise SipProtocolError("cloud market-data event envelope is invalid")
    sequence = value.get("sequence")
    raw_event = value.get("event")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
        or not isinstance(raw_event, dict)
    ):
        raise SipProtocolError("cloud market-data event envelope is invalid")
    event_type = raw_event.get("event_type")
    model: type[SipBar] | type[SipQuote]
    if event_type == "bar":
        model = SipBar
    elif event_type == "quote":
        model = SipQuote
    else:
        raise SipProtocolError("cloud market-data event type is invalid")
    try:
        event = model.model_validate(raw_event)
    except Exception as exc:
        raise SipProtocolError("cloud market-data event failed validation") from exc
    return sequence, event


def _sse_envelope(
    *,
    event_name: str | None,
    event_id: str | None,
    data_lines: list[str],
) -> tuple[int, SipEvent] | None:
    if not data_lines:
        return None
    if event_name not in {None, "market-data"}:
        return None
    try:
        payload = json.loads("\n".join(data_lines))
    except json.JSONDecodeError as exc:
        raise SipProtocolError("cloud market-data SSE payload is invalid") from exc
    sequence, event = _event_from_envelope(payload)
    if event_id is not None:
        try:
            parsed_id = int(event_id)
        except ValueError as exc:
            raise SipProtocolError("cloud market-data SSE id is invalid") from exc
        if parsed_id != sequence:
            raise SipProtocolError("cloud market-data SSE cursor does not match payload")
    return sequence, event


class PlatformSipStream:
    def __init__(
        self,
        *,
        base_url: str,
        token: SecretStr,
        symbols: tuple[str, ...],
        poll_seconds: float = 0.25,
        client: httpx.AsyncClient | None = None,
    ):
        normalized_url = base_url.rstrip("/")
        if not normalized_url.startswith(
            ("https://", "http://127.0.0.1", "http://localhost")
        ):
            raise ValueError("cloud platform API must use HTTPS outside localhost")
        if not token.get_secret_value().strip() or poll_seconds <= 0:
            raise ValueError("market-data API token and positive poll interval are required")
        normalized = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols))
        if not normalized or any(not SYMBOL_PATTERN.fullmatch(symbol) for symbol in normalized):
            raise ValueError("at least one valid stock symbol is required")
        self.base_url = normalized_url
        self._headers = {"Authorization": f"Bearer {token.get_secret_value()}"}
        self.symbols = normalized
        self.poll_seconds = poll_seconds
        self._client = client or httpx.AsyncClient(timeout=10)
        self._owns_client = client is None
        self._after_sequence = 0

    async def _fetch(self, after: int, *, limit: int = 1000) -> tuple[int, tuple[SipEvent, ...]]:
        try:
            response = await self._client.get(
                f"{self.base_url}/{PLATFORM_API_VERSION}/market-data/events",
                params={
                    "after": after,
                    "symbols": ",".join(self.symbols),
                    "limit": limit,
                },
                headers=self._headers,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SipProtocolError("cloud market-data API request failed") from exc
        if not isinstance(payload, dict) or payload.get("api_version") != PLATFORM_API_VERSION:
            raise SipProtocolError("cloud market-data API contract is invalid")
        raw_events = payload.get("events")
        next_sequence = payload.get("next_sequence")
        if not isinstance(raw_events, list) or not isinstance(next_sequence, int):
            raise SipProtocolError("cloud market-data API event page is invalid")
        events: list[SipEvent] = []
        for envelope in raw_events:
            sequence, event = _event_from_envelope(envelope)
            if sequence <= after or sequence > next_sequence:
                raise SipProtocolError("cloud market-data event cursor is invalid")
            events.append(event)
        return next_sequence, tuple(events)

    async def _stream(
        self, after: int
    ) -> AsyncGenerator[tuple[int, SipEvent], None]:
        try:
            async with self._client.stream(
                "GET",
                f"{self.base_url}/{PLATFORM_API_VERSION}/market-data/stream",
                params={
                    "after": after,
                    "symbols": ",".join(self.symbols),
                    "heartbeat_seconds": 5,
                },
                headers={
                    **self._headers,
                    "Accept": "text/event-stream",
                    "Last-Event-ID": str(after),
                },
                timeout=httpx.Timeout(15.0, connect=10.0),
            ) as response:
                if response.status_code in {404, 405}:
                    raise _LegacyStreamUnsupported
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").lower()
                if not content_type.startswith("text/event-stream"):
                    raise SipProtocolError(
                        "cloud market-data SSE content type is invalid"
                    )
                event_name: str | None = None
                event_id: str | None = None
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if line == "":
                        envelope = _sse_envelope(
                            event_name=event_name,
                            event_id=event_id,
                            data_lines=data_lines,
                        )
                        event_name = None
                        event_id = None
                        data_lines = []
                        if envelope is None:
                            continue
                        sequence, event = envelope
                        if sequence > after:
                            yield sequence, event
                        continue
                    if line.startswith(":"):
                        continue
                    field, separator, raw_value = line.partition(":")
                    value = raw_value[1:] if separator and raw_value.startswith(" ") else raw_value
                    if field == "event":
                        event_name = value
                    elif field == "id":
                        event_id = value
                    elif field == "data":
                        data_lines.append(value)
                envelope = _sse_envelope(
                    event_name=event_name,
                    event_id=event_id,
                    data_lines=data_lines,
                )
                if envelope is not None and envelope[0] > after:
                    yield envelope
        except _LegacyStreamUnsupported:
            raise
        except httpx.HTTPStatusError as exc:
            raise SipProtocolError("cloud market-data SSE request failed") from exc

    async def market_status(self) -> MarketDataHealth:
        try:
            response = await self._client.get(
                f"{self.base_url}/{PLATFORM_API_VERSION}/market-data/status",
                params={"symbols": ",".join(self.symbols)},
                headers=self._headers,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SipProtocolError("cloud market-data health request failed") from exc
        try:
            health = MarketDataHealth.model_validate(payload)
        except Exception as exc:
            raise SipProtocolError("cloud market-data health contract is invalid") from exc
        returned = tuple(item.symbol.strip().upper() for item in health.symbols)
        if (
            health.api_version != PLATFORM_API_VERSION
            or len(returned) != len(self.symbols)
            or set(returned) != set(self.symbols)
            or any(not SYMBOL_PATTERN.fullmatch(symbol) for symbol in returned)
        ):
            raise SipProtocolError("cloud market-data health contract is invalid")
        return health

    async def require_healthy(self) -> MarketDataHealth:
        health = await self.market_status()
        usable_statuses = {"healthy", "market_closed"}
        if (
            health.fallback_recommended
            or health.status not in {"healthy", "market_closed"}
            or any(
                not item.subscribed or item.status not in usable_statuses
                for item in health.symbols
            )
        ):
            raise SipProtocolError("cloud market-data health is not usable")
        return health

    async def wait_until_healthy(
        self, *, timeout_seconds: float = 60.0
    ) -> MarketDataHealth:
        if timeout_seconds <= 0:
            raise ValueError("health wait timeout must be positive")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            try:
                return await self.require_healthy()
            except SipProtocolError:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise SipProtocolError(
                        "cloud market-data health did not become usable"
                    ) from None
                await asyncio.sleep(min(1.0, remaining))

    async def probe(self, *, timeout_seconds: float = 20.0) -> SipSubscription:
        async with asyncio.timeout(timeout_seconds):
            await self._fetch(0, limit=1)
        return SipSubscription(
            connected=True,
            authenticated=True,
            bars=self.symbols,
            quotes=self.symbols,
        )

    async def ensure_subscription(
        self,
        *,
        replay_from_utc: datetime,
        expires_at_utc: datetime,
    ) -> None:
        if (
            replay_from_utc.tzinfo is None
            or replay_from_utc.utcoffset() != timedelta(0)
            or expires_at_utc.tzinfo is None
            or expires_at_utc.utcoffset() != timedelta(0)
            or expires_at_utc <= replay_from_utc
        ):
            raise ValueError("subscription timestamps must define a valid UTC window")
        try:
            response = await self._client.post(
                f"{self.base_url}/{PLATFORM_API_VERSION}/market-data/subscriptions",
                json={
                    "symbols": list(self.symbols),
                    "replay_from_utc": replay_from_utc.isoformat(),
                    "expires_at_utc": expires_at_utc.isoformat(),
                },
                headers=self._headers,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SipProtocolError("cloud market-data subscription failed") from exc
        if not isinstance(payload, dict) or payload.get("api_version") != PLATFORM_API_VERSION:
            raise SipProtocolError("cloud market-data subscription contract is invalid")
        symbols = payload.get("symbols")
        start_after = payload.get("start_after_sequence")
        if (
            not isinstance(symbols, list)
            or any(not isinstance(symbol, str) for symbol in symbols)
            or set(symbols) != set(self.symbols)
            or len(symbols) != len(self.symbols)
            or not isinstance(start_after, int)
            or start_after < 0
        ):
            raise SipProtocolError("cloud market-data subscription response is invalid")
        self._after_sequence = start_after

    async def events(self) -> AsyncGenerator[SipEvent, None]:
        after = self._after_sequence
        use_cursor_polling = False
        try:
            while True:
                if use_cursor_polling:
                    after, events = await self._fetch(after)
                    self._after_sequence = after
                    if not events:
                        await asyncio.sleep(self.poll_seconds)
                        continue
                    for event in events:
                        yield event
                    continue
                try:
                    async for sequence, event in self._stream(after):
                        after = sequence
                        self._after_sequence = sequence
                        yield event
                except _LegacyStreamUnsupported:
                    use_cursor_polling = True
                    continue
                except httpx.TransportError:
                    await asyncio.sleep(self.poll_seconds)
                    continue
                await asyncio.sleep(self.poll_seconds)
        finally:
            if self._owns_client:
                await self._client.aclose()
