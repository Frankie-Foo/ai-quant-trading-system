"""Fixed-endpoint Alpaca SIP proxy probe for the macOS research client."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from types import TracebackType
from typing import Protocol
from urllib.parse import urlparse

from pydantic import SecretStr
from websockets.asyncio.client import connect

from execution.alpaca_sip_stream import (
    SYMBOL_PATTERN,
    SipEvent,
    parse_market_data_frame,
)

ALPACA_PROXY_SIP_URL = "wss://alpaca-trade-api.vertu.cn/v2/sip"


class _WebSocket(Protocol):
    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...


class _WebSocketContext(Protocol):
    async def __aenter__(self) -> _WebSocket: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


Connector = Callable[..., _WebSocketContext]


class AlpacaProxyStreamError(RuntimeError):
    """Sanitized proxy connection, authentication, or subscription failure."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _result(*, healthy: bool, reason: str | None) -> dict[str, object]:
    host = urlparse(ALPACA_PROXY_SIP_URL).hostname
    if not host:
        raise RuntimeError("Alpaca proxy endpoint host is invalid")
    return {
        "schema_version": "alpaca_proxy_sip_probe.v1",
        "healthy": healthy,
        "reason": reason,
        "endpoint_host": host,
        "capabilities": ["bars", "quotes", "trades"],
    }


def _items(frame: str | bytes) -> list[dict[str, object]]:
    text = frame.decode("utf-8") if isinstance(frame, bytes) else frame
    parsed = json.loads(text)
    if not isinstance(parsed, list) or not all(
        isinstance(item, dict) for item in parsed
    ):
        raise ValueError("Alpaca proxy frame contract is invalid")
    return parsed


def _success(frame: str | bytes, message: str) -> bool:
    return any(
        item.get("T") == "success" and item.get("msg") == message
        for item in _items(frame)
    )


def _subscribed(frame: str | bytes, symbol: str) -> bool:
    for item in _items(frame):
        if item.get("T") != "subscription":
            continue
        for channel in ("bars", "quotes", "trades"):
            symbols = item.get(channel)
            if not isinstance(symbols, list) or symbol not in symbols:
                return False
        return True
    return False


def _symbols(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(sorted({value.strip().upper() for value in values}))
    if not normalized or any(
        not SYMBOL_PATTERN.fullmatch(symbol) for symbol in normalized
    ):
        raise ValueError("Alpaca proxy symbols are invalid")
    return normalized


async def _authenticate_and_subscribe(
    websocket: _WebSocket,
    *,
    key: str,
    secret: str,
    symbols: tuple[str, ...],
    timeout_seconds: float,
) -> None:
    connected = await asyncio.wait_for(
        websocket.recv(), timeout=timeout_seconds
    )
    if not _success(connected, "connected"):
        raise AlpacaProxyStreamError("connection_rejected")

    await websocket.send(
        json.dumps({"action": "auth", "key": key, "secret": secret})
    )
    authenticated = await asyncio.wait_for(
        websocket.recv(), timeout=timeout_seconds
    )
    if not _success(authenticated, "authenticated"):
        raise AlpacaProxyStreamError("authentication_failed")

    await websocket.send(
        json.dumps(
            {
                "action": "subscribe",
                "bars": list(symbols),
                "quotes": list(symbols),
                "trades": list(symbols),
            }
        )
    )
    subscription = await asyncio.wait_for(
        websocket.recv(), timeout=timeout_seconds
    )
    if not all(_subscribed(subscription, symbol) for symbol in symbols):
        raise AlpacaProxyStreamError("subscription_rejected")


class AlpacaProxySipStream:
    """Typed continuous quote, trade, and minute-bar stream."""

    def __init__(
        self,
        *,
        key_id: SecretStr,
        secret_key: SecretStr,
        symbols: tuple[str, ...],
        connector: Connector = connect,
        timeout_seconds: float = 10.0,
    ):
        self._key = key_id.get_secret_value().strip()
        self._secret = secret_key.get_secret_value().strip()
        if not self._key or not self._secret:
            raise ValueError("Alpaca proxy credentials are required")
        self.symbols = _symbols(symbols)
        self._connector = connector
        self._timeout_seconds = timeout_seconds

    async def events(self) -> AsyncGenerator[SipEvent, None]:
        try:
            async with self._connector(
                ALPACA_PROXY_SIP_URL,
                open_timeout=self._timeout_seconds,
                close_timeout=min(self._timeout_seconds, 5.0),
            ) as websocket:
                await _authenticate_and_subscribe(
                    websocket,
                    key=self._key,
                    secret=self._secret,
                    symbols=self.symbols,
                    timeout_seconds=self._timeout_seconds,
                )
                while True:
                    frame = await websocket.recv()
                    for event in parse_market_data_frame(frame):
                        yield event
        except AlpacaProxyStreamError:
            raise
        except Exception as exc:
            raise AlpacaProxyStreamError("connection_failed") from exc


async def probe_alpaca_proxy_sip(
    *,
    key_id: SecretStr,
    secret_key: SecretStr,
    connector: Connector = connect,
    symbol: str = "AAPL",
    timeout_seconds: float = 10.0,
) -> dict[str, object]:
    """Authenticate and verify quote, trade, and bar entitlement."""

    key = key_id.get_secret_value().strip()
    secret = secret_key.get_secret_value().strip()
    if not key or not secret:
        return _result(healthy=False, reason="credentials_missing")

    try:
        async with connector(
            ALPACA_PROXY_SIP_URL,
            open_timeout=timeout_seconds,
            close_timeout=min(timeout_seconds, 5.0),
        ) as websocket:
            await _authenticate_and_subscribe(
                websocket,
                key=key,
                secret=secret,
                symbols=_symbols((symbol,)),
                timeout_seconds=timeout_seconds,
            )
    except AlpacaProxyStreamError as exc:
        return _result(healthy=False, reason=exc.reason)
    except Exception:
        return _result(healthy=False, reason="connection_failed")

    return _result(healthy=True, reason=None)
