from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from execution.alpaca_paper import (
    AlpacaPaperBroker,
    BrokerWritesDisabledError,
    PaperCloseRequest,
    PaperOrderRequest,
)


def _broker(
    handler: Callable[[httpx.Request], httpx.Response], *, writes: bool = True
) -> AlpacaPaperBroker:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return AlpacaPaperBroker(
        api_key="test-key",
        api_secret="test-secret",
        writes_enabled=writes,
        client=client,
    )


def _request() -> PaperOrderRequest:
    return PaperOrderRequest(
        client_order_id="tsv2-orb5-20260721-AAPL-001-entry",
        symbol="AAPL",
        qty=10,
        take_profit_price="229.00",
        stop_loss_price="223.00",
    )


def test_broker_rejects_any_non_paper_endpoint() -> None:
    with pytest.raises(ValueError, match="paper endpoint"):
        AlpacaPaperBroker(
            api_key="x",
            api_secret="y",
            base_url="https://api.alpaca.markets",
        )


def test_writes_are_disabled_by_default_without_network_call() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    broker = _broker(handler, writes=False)
    with pytest.raises(BrokerWritesDisabledError):
        broker.submit_order_idempotent(_request())
    assert calls == 0


def test_submit_is_idempotent_by_client_order_id() -> None:
    posts = 0
    stored: dict[str, object] | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts, stored
        assert request.headers["APCA-API-KEY-ID"] == "test-key"
        if request.method == "GET" and request.url.path.endswith(":by_client_order_id"):
            if stored is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=stored)
        if request.method == "POST" and request.url.path == "/v2/orders":
            posts += 1
            payload = json.loads(request.content)
            assert payload["side"] == "buy"
            assert payload["order_class"] == "bracket"
            assert payload["extended_hours"] is False
            stored = {
                "id": "broker-order-1",
                "client_order_id": payload["client_order_id"],
                "symbol": payload["symbol"],
                "qty": payload["qty"],
                "filled_qty": "0",
                "status": "new",
            }
            return httpx.Response(200, json=stored)
        return httpx.Response(500)

    broker = _broker(handler)
    first = broker.submit_order_idempotent(_request())
    second = broker.submit_order_idempotent(_request())

    assert first.id == second.id == "broker-order-1"
    assert posts == 1


def test_account_probe_is_read_only_and_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "status": "ACTIVE",
                "account_blocked": False,
                "trading_blocked": False,
                "equity": "100000",
                "last_equity": "100000",
                "buying_power": "400000",
            },
        )

    account = _broker(handler, writes=False).get_account()
    assert account.status == "ACTIVE"
    assert account.trading_blocked is False
    assert account.equity == "100000"


def test_position_probe_normalizes_symbols() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v2/positions"
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "aapl",
                    "qty": "10",
                    "side": "long",
                    "market_value": "2250",
                }
            ],
        )

    positions = _broker(handler, writes=False).list_positions()
    assert positions[0].symbol == "AAPL"


def test_time_exit_cancel_and_sell_are_paper_only_and_idempotent() -> None:
    posts = 0
    stored: dict[str, object] | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts, stored
        if request.method == "DELETE":
            assert request.url.path == "/v2/orders/protective-leg"
            return httpx.Response(204)
        if request.method == "GET" and request.url.path.endswith(":by_client_order_id"):
            return (
                httpx.Response(404, json={"message": "not found"})
                if stored is None
                else httpx.Response(200, json=stored)
            )
        if request.method == "POST":
            posts += 1
            payload = json.loads(request.content)
            assert payload["side"] == "sell"
            assert payload["type"] == "market"
            assert "order_class" not in payload
            stored = {
                "id": "time-exit-1",
                "client_order_id": payload["client_order_id"],
                "symbol": payload["symbol"],
                "qty": payload["qty"],
                "filled_qty": "0",
                "status": "new",
            }
            return httpx.Response(200, json=stored)
        return httpx.Response(500)

    broker = _broker(handler)
    request = PaperCloseRequest(
        client_order_id="tsv2-time-exit-abc123",
        symbol="AAPL",
        qty=10,
    )
    assert broker.cancel_order("protective-leg") is True
    first = broker.submit_close_order_idempotent(request)
    replay = broker.submit_close_order_idempotent(request)
    assert first == replay
    assert posts == 1
