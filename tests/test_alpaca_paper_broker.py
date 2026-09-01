from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from execution.alpaca_paper import (
    BrokerWritesDisabledError,
    CloudPaperBroker,
    DirectAlpacaPaperBroker,
    FreshNbboQuote,
    PaperCloseRequest,
    PaperExtendedLimitRequest,
    PaperOrderRequest,
    PaperStopRequest,
    ProtectedPaperEntryRequest,
    build_protected_entry,
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
                    {
                        "symbol": "aapl",
                        "qty": "10",
                        "side": "long",
                        "market_value": "2250",
                        "avg_entry_price": "220.50",
                        "current_price": "225.00",
                    }
                ],
            },
        )

    broker = _broker(handler, writes=False)
    assert broker.get_account().status == "ACTIVE"
    position = broker.list_positions()[0]
    assert position.symbol == "AAPL"
    assert position.avg_entry_price == "220.50"
    assert position.current_price == "225.00"


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


def test_extended_hours_order_is_simple_limit_without_unsupported_bracket() -> None:
    request = PaperExtendedLimitRequest(
        client_order_id="tsv2-premarket-AAPL-entry-0",
        symbol="AAPL",
        qty=10,
        side="buy",
        limit_price="225.10",
    )

    assert request.broker_payload() == {
        "client_order_id": "tsv2-premarket-AAPL-entry-0",
        "symbol": "AAPL",
        "qty": "10",
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "extended_hours": True,
        "limit_price": "225.10",
    }


def test_regular_entry_can_attach_stop_without_forcing_full_take_profit() -> None:
    request = PaperOrderRequest(
        client_order_id="tsv2-AAPL-tail-entry",
        symbol="AAPL",
        qty=5,
        stop_loss_price="223.00",
    )

    assert request.broker_payload() == {
        "client_order_id": "tsv2-AAPL-tail-entry",
        "symbol": "AAPL",
        "qty": "5",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "extended_hours": False,
        "order_class": "oto",
        "stop_loss": {"stop_price": "223.00"},
    }


def test_regular_entry_can_be_submitted_without_attached_legs() -> None:
    request = PaperOrderRequest(
        client_order_id="tsv2-AAPL-probe-entry",
        symbol="AAPL",
        qty=5,
    )

    assert request.broker_payload() == {
        "client_order_id": "tsv2-AAPL-probe-entry",
        "symbol": "AAPL",
        "qty": "5",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "extended_hours": False,
    }


def test_take_profit_entry_requires_a_protective_stop() -> None:
    with pytest.raises(ValueError, match="requires stop_loss_price"):
        PaperOrderRequest(
            client_order_id="tsv2-AAPL-invalid-entry",
            symbol="AAPL",
            qty=5,
            take_profit_price="230.00",
        )


def test_protected_entry_is_a_marketable_limit_bracket_with_three_r_target() -> None:
    now = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)
    request = build_protected_entry(
        client_order_id="mm-20260824-AAPL-entry-1",
        symbol="AAPL",
        qty=100,
        signal_reference=Decimal("100.00"),
        structural_stop=Decimal("98.55"),
        quote=FreshNbboQuote(
            symbol="AAPL",
            bid=Decimal("100.00"),
            ask=Decimal("100.05"),
            asof_utc=now - timedelta(milliseconds=100),
            feed="sip",
        ),
        observed_at_utc=now,
        stop_slippage_reserve=Decimal("0.005"),
    )

    assert isinstance(request, ProtectedPaperEntryRequest)
    assert request.broker_payload() == {
        "client_order_id": "mm-20260824-AAPL-entry-1",
        "symbol": "AAPL",
        "qty": "100",
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "extended_hours": False,
        "limit_price": "100.05",
        "order_class": "bracket",
        "stop_loss": {"stop_price": "98.55"},
        "take_profit": {"limit_price": "104.55"},
    }


def test_protected_entry_allows_a_point_two_percent_immediate_spread() -> None:
    now = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)

    request = build_protected_entry(
        client_order_id="mm-20260824-AAPL-entry-wide",
        symbol="AAPL",
        qty=10,
        signal_reference=Decimal("100.00"),
        structural_stop=Decimal("98.50"),
        quote=FreshNbboQuote(
            symbol="AAPL",
            bid=Decimal("100.00"),
            ask=Decimal("100.20"),
            asof_utc=now - timedelta(milliseconds=100),
            feed="sip",
        ),
        observed_at_utc=now,
    )

    assert request.limit_price == "100.20"


@pytest.mark.parametrize(
    ("bid", "ask", "age_seconds", "feed", "message"),
    [
        ("99.80", "100.05", 0.1, "sip", "spread"),
        ("100.25", "100.26", 0.1, "sip", "slippage"),
        ("100.00", "100.05", 3.0, "sip", "stale"),
        ("100.00", "100.05", 0.1, "iex", "SIP"),
    ],
)
def test_protected_entry_rejects_bad_immediate_nbbo(
    bid: str,
    ask: str,
    age_seconds: float,
    feed: str,
    message: str,
) -> None:
    now = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match=message):
        build_protected_entry(
            client_order_id="mm-20260824-AAPL-entry-1",
            symbol="AAPL",
            qty=100,
            signal_reference=Decimal("100.00"),
            structural_stop=Decimal("98.50"),
            quote=FreshNbboQuote(
                symbol="AAPL",
                bid=Decimal(bid),
                ask=Decimal(ask),
                asof_utc=now - timedelta(seconds=age_seconds),
                feed=feed,
            ),
            observed_at_utc=now,
        )


def test_regular_protective_stop_is_sell_only_and_cannot_open_a_long() -> None:
    request = PaperStopRequest(
        client_order_id="tsv2-AAPL-premarket-handoff",
        symbol="AAPL",
        qty=10,
        stop_price="223.00",
    )

    assert request.broker_payload() == {
        "client_order_id": "tsv2-AAPL-premarket-handoff",
        "symbol": "AAPL",
        "qty": "10",
        "side": "sell",
        "type": "stop",
        "time_in_force": "day",
        "extended_hours": False,
        "stop_price": "223.00",
    }


def test_extended_hours_order_uses_dedicated_scoped_gateway_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path == "/v1/paper/orders"
        assert payload["kind"] == "extended_limit"
        assert payload["request"]["extended_hours"] is True
        assert payload["request"]["side"] == "sell"
        return httpx.Response(
            200,
            json={
                "api_version": "v1",
                "order": {
                    "id": "synthetic-stop-1",
                    "client_order_id": payload["request"]["client_order_id"],
                    "symbol": "AAPL",
                    "qty": 10,
                    "filled_qty": "0",
                    "status": "new",
                },
            },
        )

    order = _broker(handler).submit_extended_limit_idempotent(
        PaperExtendedLimitRequest(
            client_order_id="tsv2-premarket-AAPL-stop-0",
            symbol="AAPL",
            qty=10,
            side="sell",
            limit_price="221.50",
        )
    )

    assert order.id == "synthetic-stop-1"


def _direct_broker(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    writes: bool = True,
) -> DirectAlpacaPaperBroker:
    return DirectAlpacaPaperBroker(
        key_id=SecretStr("paper-key-id"),
        secret_key=SecretStr("paper-secret"),
        writes_enabled=writes,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_direct_adapter_is_pinned_to_alpaca_paper_host() -> None:
    with pytest.raises(ValueError, match="Paper host"):
        DirectAlpacaPaperBroker(
            key_id=SecretStr("paper-key-id"),
            secret_key=SecretStr("paper-secret"),
            base_url="https://api.alpaca.markets",
        )


def test_direct_adapter_reads_account_positions_and_open_orders() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["APCA-API-KEY-ID"] == "paper-key-id"
        assert request.headers["APCA-API-SECRET-KEY"] == "paper-secret"
        if request.url.path == "/v2/account":
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
        if request.url.path == "/v2/positions":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "aapl",
                        "qty": "10",
                        "side": "long",
                        "market_value": "2250",
                        "avg_entry_price": "220.50",
                        "current_price": "225.00",
                    }
                ],
            )
        assert request.url.path == "/v2/orders"
        assert request.url.params["status"] == "open"
        assert request.url.params["nested"] == "true"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "working-1",
                    "client_order_id": "tsv2-owned",
                    "symbol": "AAPL",
                    "qty": "10",
                    "filled_qty": "0",
                    "status": "new",
                }
            ],
        )

    broker = _direct_broker(handler, writes=False)

    assert broker.get_account().equity == "100000"
    assert broker.list_positions()[0].symbol == "AAPL"
    assert broker.list_open_orders()[0].id == "working-1"


def test_direct_adapter_entry_is_idempotent_before_posting() -> None:
    post_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_calls
        if request.method == "GET":
            assert request.url.path == "/v2/orders:by_client_order_id"
            return httpx.Response(
                200,
                json={
                    "id": "existing-1",
                    "client_order_id": _request().client_order_id,
                    "symbol": "AAPL",
                    "qty": "10",
                    "filled_qty": "0",
                    "status": "new",
                },
            )
        post_calls += 1
        return httpx.Response(500)

    order = _direct_broker(handler).submit_order_idempotent(_request())

    assert order.id == "existing-1"
    assert post_calls == 0


def test_direct_adapter_submits_structured_paper_orders_and_cancels() -> None:
    submitted: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "GET"
            and request.url.path == "/v2/orders:by_client_order_id"
        ):
            return httpx.Response(404)
        if request.method == "DELETE":
            assert request.url.path == "/v2/orders/working-1"
            return httpx.Response(204)
        payload = json.loads(request.content)
        submitted.append(payload)
        return httpx.Response(
            200,
            json={
                "id": f"submitted-{len(submitted)}",
                "client_order_id": payload["client_order_id"],
                "symbol": payload["symbol"],
                "qty": payload["qty"],
                "filled_qty": "0",
                "status": "new",
            },
        )

    broker = _direct_broker(handler)
    bracket = broker.submit_order_idempotent(_request())
    extended = broker.submit_extended_limit_idempotent(
        PaperExtendedLimitRequest(
            client_order_id="tsv2-AAPL-extended-1",
            symbol="AAPL",
            qty=5,
            side="sell",
            limit_price="221.50",
        )
    )

    assert bracket.id == "submitted-1"
    assert submitted[0]["order_class"] == "bracket"
    assert submitted[0]["extended_hours"] is False
    assert extended.id == "submitted-2"
    assert submitted[1]["extended_hours"] is True
    assert submitted[1]["type"] == "limit"
    assert broker.cancel_order("working-1") is True


def test_direct_adapter_submits_protected_entry_as_one_bracket() -> None:
    submitted: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.params["nested"] == "true"
            return httpx.Response(404)
        payload = json.loads(request.content)
        submitted.append(payload)
        return httpx.Response(
            201,
            json={
                "id": "protected-parent",
                "client_order_id": payload["client_order_id"],
                "symbol": payload["symbol"],
                "qty": payload["qty"],
                "filled_qty": "0",
                "status": "new",
                "legs": [
                    {
                        "id": "stop-child",
                        "client_order_id": "stop-child-client",
                        "symbol": "AAPL",
                        "qty": "100",
                        "filled_qty": "0",
                        "status": "held",
                        "side": "sell",
                        "type": "stop",
                        "legs": None,
                    }
                ],
            },
        )

    request = build_protected_entry(
        client_order_id="mm-20260824-AAPL-entry-1",
        symbol="AAPL",
        qty=100,
        signal_reference=Decimal("100.00"),
        structural_stop=Decimal("98.55"),
        quote=FreshNbboQuote(
            symbol="AAPL",
            bid=Decimal("100.00"),
            ask=Decimal("100.05"),
            asof_utc=datetime(2026, 8, 24, 14, 0, tzinfo=UTC),
            feed="sip",
        ),
        observed_at_utc=datetime(2026, 8, 24, 14, 0, tzinfo=UTC),
        stop_slippage_reserve=Decimal("0.005"),
    )
    order = _direct_broker(handler).submit_protected_entry_idempotent(request)

    assert submitted[0]["type"] == "limit"
    assert submitted[0]["order_class"] == "bracket"
    assert order.legs[0].order_type == "stop"


def test_direct_adapter_write_gate_blocks_before_idempotency_network_read() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    broker = _direct_broker(handler, writes=False)
    with pytest.raises(BrokerWritesDisabledError):
        broker.submit_order_idempotent(_request())
    assert calls == 0
