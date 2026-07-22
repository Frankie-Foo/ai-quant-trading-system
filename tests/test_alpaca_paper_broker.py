from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from execution.alpaca_paper import (
    BrokerWritesDisabledError,
    CloudPaperBroker,
    PaperCloseRequest,
    PaperOrderRequest,
)


def _broker(
    handler: Callable[[httpx.Request], httpx.Response], *, writes: bool = True
) -> CloudPaperBroker:
    return CloudPaperBroker(
        base_url="http://localhost:8765",
        token=SecretStr("paper-service-token"),
        writes_enabled=writes,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _request() -> PaperOrderRequest:
    return PaperOrderRequest(
        client_order_id="tsv2-orb5-20260721-AAPL-001-entry",
        symbol="AAPL",
        qty=10,
        take_profit_price="229.00",
        stop_loss_price="223.00",
    )


def test_broker_rejects_insecure_nonlocal_platform_endpoint() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        CloudPaperBroker(
            base_url="http://example.com",
            token=SecretStr("token"),
        )


def test_writes_are_disabled_locally_without_network_call() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    broker = _broker(handler, writes=False)
    with pytest.raises(BrokerWritesDisabledError):
        broker.submit_order_idempotent(_request())
    assert calls == 0


def test_entry_order_uses_scoped_api_token_and_structured_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer paper-service-token"
        assert request.url.path == "/v1/paper/orders"
        payload = json.loads(request.content)
        assert payload["kind"] == "entry"
        assert payload["request"]["side"] == "buy"
        return httpx.Response(
            200,
            json={
                "api_version": "v1",
                "order": {
                    "id": "broker-order-1",
                    "client_order_id": payload["request"]["client_order_id"],
                    "symbol": "AAPL",
                    "qty": 10,
                    "filled_qty": "0",
                    "status": "new",
                },
            },
        )

    order = _broker(handler).submit_order_idempotent(_request())
    assert order.id == "broker-order-1"


def test_account_and_positions_are_read_only_api_calls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/paper/account":
            return httpx.Response(
                200,
                json={
                    "api_version": "v1",
                    "account": {
                        "status": "ACTIVE",
                        "account_blocked": False,
                        "trading_blocked": False,
                        "equity": "100000",
                        "last_equity": "100000",
                        "buying_power": "400000",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "api_version": "v1",
                "positions": [
                    {"symbol": "aapl", "qty": "10", "side": "long", "market_value": "2250"}
                ],
            },
        )

    broker = _broker(handler, writes=False)
    assert broker.get_account().status == "ACTIVE"
    assert broker.list_positions()[0].symbol == "AAPL"


def test_time_exit_cancel_and_close_use_paper_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            assert request.url.path == "/v1/paper/orders/cancel/protective-leg"
            return httpx.Response(200, json={"api_version": "v1", "cancelled": True})
        payload = json.loads(request.content)
        assert payload["kind"] == "close"
        return httpx.Response(
            200,
            json={
                "api_version": "v1",
                "order": {
                    "id": "time-exit-1",
                    "client_order_id": payload["request"]["client_order_id"],
                    "symbol": "AAPL",
                    "qty": 10,
                    "filled_qty": "0",
                    "status": "new",
                },
            },
        )

    broker = _broker(handler)
    assert broker.cancel_order("protective-leg") is True
    order = broker.submit_close_order_idempotent(
        PaperCloseRequest(client_order_id="tsv2-time-exit-abc123", symbol="AAPL", qty=10)
    )
    assert order.id == "time-exit-1"
