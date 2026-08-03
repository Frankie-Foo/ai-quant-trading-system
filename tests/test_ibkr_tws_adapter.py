from __future__ import annotations

import threading
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from execution.ibkr_execution import BrokerOrderRejected, BrokerOrderRequest
from execution.ibkr_tws_adapter import (
    BrokerConnectionTimeout,
    BrokerGatewayUnavailable,
    OfficialIbapiAdapter,
)


class BlockingConnection:
    def __init__(self) -> None:
        self.closed = threading.Event()

    def disconnect(self) -> None:
        self.closed.set()


class BlockingClient:
    def __init__(self) -> None:
        self.conn = BlockingConnection()
        self.connect_returned = threading.Event()
        self.args: tuple[str, int, int] | None = None

    def connect(self, host: str, port: int, client_id: int) -> None:
        self.args = (host, port, client_id)
        self.conn.closed.wait(1)
        self.connect_returned.set()

    def isConnected(self) -> bool:
        return False


def test_gateway_handshake_timeout_closes_socket_and_does_not_leak_connect_thread() -> None:
    client = BlockingClient()
    adapter = OfficialIbapiAdapter(
        connect_timeout=0.02,
        shutdown_grace=0.2,
        client_factory=lambda _wrapper: client,
    )

    with pytest.raises(BrokerConnectionTimeout, match="connection_timeout"):
        adapter.connect(host="172.18.0.1", port=4001, client_id=71)

    assert client.args == ("172.18.0.1", 4001, 71)
    assert client.conn.closed.is_set()
    assert client.connect_returned.wait(0.2)
    assert adapter.connected is False


def test_official_adapter_refuses_every_non_live_port() -> None:
    client = BlockingClient()
    adapter = OfficialIbapiAdapter(
        client_factory=lambda _wrapper: client,
    )

    with pytest.raises(ValueError, match="4001"):
        adapter.connect(host="127.0.0.1", port=4002, client_id=71)

    assert client.args is None


class UnavailableClient:
    def __init__(self, wrapper: Any) -> None:
        self.wrapper = wrapper

    def connect(self, host: str, port: int, client_id: int) -> None:
        del host, port, client_id
        self.wrapper.error(-1, 502, "Couldn't connect to TWS")

    def isConnected(self) -> bool:
        return False


def test_ibapi_502_is_reported_as_gateway_unavailable() -> None:
    adapter = OfficialIbapiAdapter(
        connect_timeout=0.1,
        client_factory=lambda wrapper: UnavailableClient(wrapper),
    )

    with pytest.raises(BrokerGatewayUnavailable, match="gateway_unavailable"):
        adapter.connect(host="192.168.151.141", port=4001, client_id=71)

    assert adapter.connected is False


class ResponsiveClient:
    def __init__(self, wrapper: Any) -> None:
        self.wrapper = wrapper
        self.connected = False
        self.stop = threading.Event()
        self.run_returned = threading.Event()
        self.placed: list[tuple[int, Any, Any]] = []
        self.live_orders: list[tuple[int, Any, Any, Any]] = []
        self.completed_orders: list[tuple[Any, Any, Any]] = []

    def connect(self, host: str, port: int, client_id: int) -> None:
        del host, port, client_id
        self.connected = True
        self.wrapper.error(-1, 2107, "HMDS data farm connection is inactive")
        self.wrapper.error(-1, 2108, "Market data farm connection is inactive")
        self.wrapper.error(-1, 2119, "Market data farm is inactive but available")
        self.wrapper.error(-1, 2103, "Market data farm connection is broken")
        self.wrapper.error(-1, 2105, "HMDS data farm connection is broken")
        self.wrapper.managedAccounts("U7654321")
        self.wrapper.nextValidId(8100)

    def isConnected(self) -> bool:
        return self.connected

    def run(self) -> None:
        self.stop.wait(1)
        self.run_returned.set()

    def disconnect(self) -> None:
        self.connected = False
        self.stop.set()

    def reqPositions(self) -> None:
        contract = SimpleNamespace(symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
        self.wrapper.position("U7654321", contract, Decimal("3"), 190.0)
        self.wrapper.positionEnd()

    def reqAllOpenOrders(self) -> None:
        for args in self.live_orders:
            self.wrapper.openOrder(*args)
        self.wrapper.openOrderEnd()

    def reqCompletedOrders(self, api_only: bool) -> None:
        assert api_only is True
        for args in self.completed_orders:
            self.wrapper.completedOrder(*args)
        self.wrapper.completedOrdersEnd()

    def placeOrder(self, order_id: int, contract: Any, order: Any) -> None:
        self.placed.append((order_id, contract, order))
        state = SimpleNamespace(
            commission=1.1,
            initMarginChange="400",
            warningText="price is far from market",
            status="PreSubmitted",
        )
        order.permId = 60000 + order_id
        self.wrapper.error(order_id, 399, "Order message warning")
        self.wrapper.openOrder(order_id, contract, order, state)
        if not order.whatIf:
            self.live_orders = [(order_id, contract, order, state)]
            self.wrapper.orderStatus(
                order_id,
                "Submitted",
                0,
                order.totalQuantity,
                0,
                order.permId,
                0,
                0,
                71,
                "",
                0,
            )


def test_official_adapter_maps_account_what_if_submission_and_nonfatal_status_codes() -> None:
    clients: list[ResponsiveClient] = []

    def factory(wrapper: Any) -> ResponsiveClient:
        client = ResponsiveClient(wrapper)
        clients.append(client)
        return client

    adapter = OfficialIbapiAdapter(
        connect_timeout=0.2,
        request_timeout=0.2,
        client_factory=factory,
    )
    adapter.connect(host="127.0.0.1", port=4001, client_id=71)
    request = BrokerOrderRequest(
        client_order_id="live-1",
        account_id="U7654321",
        symbol="AAPL",
        security_type="STK",
        exchange="SMART",
        currency="USD",
        order_type="LMT",
        tif="DAY",
        side="BUY",
        operation="OpenLong",
        quantity=1,
        limit_price=Decimal("200"),
    )

    account = adapter.account_snapshot()
    what_if = adapter.what_if(request)
    submitted = adapter.submit(request, order_ref="vq:live:U7654321:71:live-1")
    submitted_contract = clients[0].placed[-1][1]
    submitted_order = clients[0].placed[-1][2]
    clients[0].live_orders = []
    clients[0].completed_orders = [
        (
            submitted_contract,
            submitted_order,
            SimpleNamespace(status="Filled", completedStatus="Filled"),
        )
    ]
    recovered = adapter.find_by_order_ref(submitted.order_ref)

    assert account.account_id == "U7654321"
    assert account.positions[0]["quantity"] == "3"
    assert account.api_read_only is False
    assert what_if.accepted is True
    assert what_if.estimated_commission == Decimal("1.1")
    assert what_if.initial_margin_change == Decimal("400")
    assert "399" in (what_if.warning or "")
    assert submitted.status == "submitted"
    assert submitted.order_id == 8101
    assert submitted.perm_id == 68101
    assert recovered is not None
    assert recovered.status == "filled"
    assert recovered.order_id == submitted.order_id
    assert recovered.perm_id == submitted.perm_id
    assert len(clients[0].placed) == 2
    assert submitted_order.eTradeOnly is False
    assert submitted_order.firmQuoteOnly is False
    clients[0].wrapper.error(-1, 1100, "Connectivity between IB and TWS has been lost")
    assert adapter.connected is False
    with pytest.raises(RuntimeError, match="not connected"):
        adapter.account_snapshot()
    adapter.disconnect()
    assert clients[0].run_returned.is_set()


class RejectingClient(ResponsiveClient):
    def placeOrder(self, order_id: int, contract: Any, order: Any) -> None:
        self.placed.append((order_id, contract, order))
        self.wrapper.error(order_id, 201, "Order rejected")


def test_official_adapter_classifies_order_error_as_deterministic_rejection() -> None:
    clients: list[RejectingClient] = []

    def factory(wrapper: Any) -> RejectingClient:
        client = RejectingClient(wrapper)
        clients.append(client)
        return client

    adapter = OfficialIbapiAdapter(
        connect_timeout=0.2,
        request_timeout=0.2,
        client_factory=factory,
    )
    adapter.connect(host="127.0.0.1", port=4001, client_id=71)
    request = BrokerOrderRequest(
        client_order_id="reject-1",
        account_id="U7654321",
        symbol="AAPL",
        security_type="STK",
        exchange="SMART",
        currency="USD",
        order_type="LMT",
        tif="DAY",
        side="BUY",
        operation="OpenLong",
        quantity=1,
        limit_price=Decimal("200"),
    )

    with pytest.raises(BrokerOrderRejected) as error:
        adapter.submit(request, order_ref="vq:live:U7654321:71:reject-1")

    assert error.value.code == "ibkr_error_201"
    adapter.disconnect()


class MultiAccountClient(ResponsiveClient):
    def connect(self, host: str, port: int, client_id: int) -> None:
        super().connect(host, port, client_id)
        self.wrapper.managedAccounts("U7654321,U9999999")


def test_multiple_managed_accounts_require_an_exact_configured_account() -> None:
    first = OfficialIbapiAdapter(
        connect_timeout=0.2,
        request_timeout=0.2,
        client_factory=lambda wrapper: MultiAccountClient(wrapper),
    )
    first.connect(host="127.0.0.1", port=4001, client_id=71)
    with pytest.raises(BrokerGatewayUnavailable, match="multiple_accounts_require_selection"):
        first.account_snapshot()
    first.disconnect()

    selected = OfficialIbapiAdapter(
        connect_timeout=0.2,
        request_timeout=0.2,
        expected_account_id="U7654321",
        client_factory=lambda wrapper: MultiAccountClient(wrapper),
    )
    selected.connect(host="127.0.0.1", port=4001, client_id=72)
    assert selected.account_snapshot().account_id == "U7654321"
    selected.disconnect()


class HangingSendClient(ResponsiveClient):
    def __init__(self, wrapper: Any) -> None:
        super().__init__(wrapper)
        self.send_release = threading.Event()

    def placeOrder(self, order_id: int, contract: Any, order: Any) -> None:
        del order_id, contract, order
        self.send_release.wait(1)

    def disconnect(self) -> None:
        self.send_release.set()
        super().disconnect()


def test_synchronous_sdk_send_is_time_bounded_and_marks_connection_lost() -> None:
    clients: list[HangingSendClient] = []

    def factory(wrapper: Any) -> HangingSendClient:
        client = HangingSendClient(wrapper)
        clients.append(client)
        return client

    adapter = OfficialIbapiAdapter(
        connect_timeout=0.2,
        request_timeout=0.02,
        shutdown_grace=0.05,
        client_factory=factory,
    )
    adapter.connect(host="127.0.0.1", port=4001, client_id=71)
    request = BrokerOrderRequest(
        client_order_id="hang-1",
        account_id="U7654321",
        symbol="AAPL",
        security_type="STK",
        exchange="SMART",
        currency="USD",
        order_type="LMT",
        tif="DAY",
        side="BUY",
        operation="OpenLong",
        quantity=1,
        limit_price=Decimal("200"),
    )
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="order_send_timeout"):
        adapter.submit(request, order_ref="vq:live:U7654321:71:hang-1")

    assert time.monotonic() - started < 0.2
    assert adapter.connected is False
    assert clients[0].send_release.is_set()
