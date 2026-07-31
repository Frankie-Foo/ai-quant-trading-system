"""Fixed-endpoint Alpaca SIP proxy probe for the macOS research client."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator, Callable
from datetime import datetime
from types import TracebackType
from typing import Protocol
from urllib.parse import urlparse

import httpx
import polars as pl
from pydantic import SecretStr
from websockets.asyncio.client import connect

from data_plane.quality import canonicalize_bars, nullable_float
from execution.alpaca_sip_stream import (
    SYMBOL_PATTERN,
    SipEvent,
    parse_market_data_frame,
)

ALPACA_PROXY_SIP_URL = "wss://alpaca-trade-api.vertu.cn/v2/sip"
ALPACA_PROXY_REST_URL = "https://alpaca-trade-api.vertu.cn/v2/stocks/bars"


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


def fetch_alpaca_proxy_bars(
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
) -> pl.DataFrame:
    if len(symbols) > 20:
        frames = [
            fetch_alpaca_proxy_bars(
                symbols[index : index + 20],
                start_utc,
                end_utc,
            )
            for index in range(0, len(symbols), 20)
        ]
        return pl.concat(frames) if frames else pl.DataFrame()
    key = os.getenv("ALPACA_PROXY_KEY", "").strip()
    secret = os.getenv("ALPACA_PROXY_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError("Alpaca proxy credentials are missing")
    rows: list[dict[str, object]] = []
    token: str | None = None
    with httpx.Client(timeout=30.0) as client:
        while True:
            params = {
                "symbols": ",".join(symbols),
                "timeframe": "1Min",
                "start": start_utc.isoformat(),
                "end": end_utc.isoformat(),
                "feed": "sip",
                "limit": "10000",
            }
            if token:
                params["page_token"] = token
            response = client.get(
                ALPACA_PROXY_REST_URL,
                params=params,
                headers={
                    "APCA-API-KEY-ID": key,
                    "APCA-API-SECRET-KEY": secret,
                },
            )
            response.raise_for_status()
            payload = response.json()
            bars = payload.get("bars", {})
            if not isinstance(bars, dict):
                raise ValueError("Alpaca proxy bars response is invalid")
            for symbol, values in bars.items():
                if not isinstance(values, list):
                    continue
                for bar in values:
                    if not isinstance(bar, dict):
                        continue
                    rows.append(
                        {
                            "symbol": symbol,
                            "ts_utc": bar.get("t"),
                            "open": bar.get("o"),
                            "high": bar.get("h"),
                            "low": bar.get("l"),
                            "close": bar.get("c"),
                            "volume": bar.get("v"),
                            "trade_count": bar.get("n"),
                            "vwap": nullable_float(bar.get("vw")),
                            "source": "alpaca_proxy.rest",
                            "feed": "sip",
                            "adjustment": "split_adjusted",
                        }
                    )
            next_token = payload.get("next_page_token")
            if not next_token:
                break
            token = str(next_token)
    if not rows:
        return canonicalize_bars(pl.DataFrame())
    return canonicalize_bars(pl.DataFrame(rows)).filter(
        (pl.col("ts_utc") >= start_utc) & (pl.col("ts_utc") < end_utc)
    )


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
