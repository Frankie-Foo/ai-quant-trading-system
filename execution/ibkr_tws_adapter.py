"""Official ``ibapi`` adapter for an already-authenticated TWS/IB Gateway.

This module never accepts an IBKR username or password.  Authentication stays in
TWS/Gateway and this adapter uses only the local socket API.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.wrapper import EWrapper

from execution.ibkr_execution import (
    LIVE_PORT,
    BrokerAccountSnapshot,
    BrokerOrderRejected,
    BrokerOrderRequest,
    BrokerSubmission,
    BrokerWhatIf,
)


class BrokerConnectionTimeout(TimeoutError):
    """The Gateway socket opened but did not complete the IB API handshake."""


class BrokerGatewayUnavailable(ConnectionError):
    """TWS/Gateway rejected or could not establish the API connection."""


class _IbkrCallbacks(EWrapper):  # type: ignore[misc]
    def __init__(self) -> None:
        super().__init__()
        self.ready = threading.Event()
        self.accounts_ready = threading.Event()
        self.connection_lost = threading.Event()
        self.next_order_id: int | None = None
        self.accounts: tuple[str, ...] = ()
        self.fatal_error: tuple[int, str] | None = None
        self.information: list[tuple[int, str]] = []
        self.warnings: list[tuple[int, str]] = []
        self.positions: list[dict[str, Any]] = []
        self.positions_done = threading.Event()
        self.open_orders: dict[int, dict[str, Any]] = {}
        self.open_orders_done = threading.Event()
        self.completed_orders: dict[int, dict[str, Any]] = {}
        self.completed_orders_done = threading.Event()
        self.order_events: dict[int, threading.Event] = {}
        self.order_results: dict[int, dict[str, Any]] = {}
        self.errors_by_request: dict[int, tuple[int, str]] = {}
        self.warnings_by_request: dict[int, list[tuple[int, str]]] = {}
        self.lock = threading.RLock()

    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self.next_order_id = orderId
        self.ready.set()

    def managedAccounts(self, accountsList: str) -> None:  # noqa: N802
        self.accounts = tuple(
            account.strip() for account in accountsList.split(",") if account.strip()
        )
        self.accounts_ready.set()

    def error(
        self,
        reqId: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson: str = "",
    ) -> None:
        del advancedOrderRejectJson
        if errorCode in {2104, 2106, 2107, 2108, 2119, 2158}:
            self.information.append((errorCode, errorString))
        elif errorCode in {399, 2103, 2105}:
            self.warnings.append((errorCode, errorString))
            if errorCode == 399:
                self.warnings_by_request.setdefault(reqId, []).append(
                    (errorCode, errorString)
                )
        else:
            self.fatal_error = (errorCode, errorString)
            self.errors_by_request[reqId] = (errorCode, errorString)
            if errorCode == 1100:
                self.connection_lost.set()
            if errorCode in {502, 1100}:
                self.ready.set()
            with self.lock:
                event = self.order_events.get(reqId)
            if event is not None:
                event.set()

    def connectionClosed(self) -> None:  # noqa: N802
        self.connection_lost.set()

    def position(
        self,
        account: str,
        contract: Any,
        position: Any,
        avgCost: float,
    ) -> None:
        with self.lock:
            self.positions.append(
                {
                    "account_id": account,
                    "symbol": str(contract.symbol),
                    "security_type": str(contract.secType),
                    "exchange": str(getattr(contract, "exchange", "")),
                    "currency": str(contract.currency),
                    "quantity": str(position),
                    "average_cost": str(avgCost),
                }
            )

    def positionEnd(self) -> None:  # noqa: N802
        self.positions_done.set()

    def openOrder(
        self,
        orderId: int,
        contract: Any,
        order: Any,
        orderState: Any,
    ) -> None:
        result = {
            "order_id": orderId,
            "perm_id": self._optional_int(getattr(order, "permId", None)),
            "order_ref": str(getattr(order, "orderRef", "")),
            "account_id": str(getattr(order, "account", "")),
            "status": str(getattr(orderState, "status", "Submitted")),
            "symbol": str(contract.symbol),
            "side": str(order.action),
            "quantity": str(order.totalQuantity),
            "limit_price": str(order.lmtPrice),
            "commission": getattr(orderState, "commission", None),
            "initial_margin_change": getattr(orderState, "initMarginChange", None),
            "warning": str(getattr(orderState, "warningText", "") or ""),
        }
        with self.lock:
            self.order_results[orderId] = result
            if not bool(getattr(order, "whatIf", False)):
                self.open_orders[orderId] = result
            event = self.order_events.get(orderId)
        if event is not None:
            event.set()

    def openOrderEnd(self) -> None:  # noqa: N802
        self.open_orders_done.set()

    def completedOrder(self, contract: Any, order: Any, orderState: Any) -> None:  # noqa: N802
        order_id = self._optional_int(getattr(order, "orderId", None)) or 0
        result = {
            "order_id": order_id,
            "perm_id": self._optional_int(getattr(order, "permId", None)),
            "order_ref": str(getattr(order, "orderRef", "")),
            "account_id": str(getattr(order, "account", "")),
            "status": str(
                getattr(orderState, "completedStatus", "")
                or getattr(orderState, "status", "")
                or "unknown"
            ),
            "symbol": str(contract.symbol),
            "side": str(order.action),
            "quantity": str(order.totalQuantity),
            "limit_price": str(order.lmtPrice),
        }
        with self.lock:
            self.completed_orders[order_id] = result

    def completedOrdersEnd(self) -> None:  # noqa: N802
        self.completed_orders_done.set()

    def orderStatus(  # noqa: N802
        self,
        orderId: int,
        status: str,
        filled: Any,
        remaining: Any,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float = 0.0,
    ) -> None:
        del filled, remaining, avgFillPrice, parentId, lastFillPrice, clientId, whyHeld
        del mktCapPrice
        with self.lock:
            result = self.order_results.setdefault(orderId, {"order_id": orderId})
            result["status"] = status
            result["perm_id"] = permId or result.get("perm_id")
            event = self.order_events.get(orderId)
        if event is not None:
            event.set()

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return None
        return normalized or None


class OfficialIbapiAdapter:
    """Thread-bounded adapter around the official synchronous ``ibapi`` client."""

    def __init__(
        self,
        *,
        connect_timeout: float = 8.0,
        request_timeout: float = 5.0,
        shutdown_grace: float = 1.0,
        api_read_only: bool = False,
        expected_account_id: str | None = None,
        client_factory: Callable[[_IbkrCallbacks], EClient] | None = None,
    ) -> None:
        if connect_timeout <= 0 or request_timeout <= 0 or shutdown_grace <= 0:
            raise ValueError("IBKR timeouts must be positive")
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.shutdown_grace = shutdown_grace
        self.api_read_only = api_read_only
        self.expected_account_id = (
            None if expected_account_id is None else expected_account_id.strip() or None
        )
        self._client_factory = client_factory or (lambda wrapper: EClient(wrapper))
        self._client: EClient | None = None
        self._callbacks: _IbkrCallbacks | None = None
        self._connect_thread: threading.Thread | None = None
        self._run_thread: threading.Thread | None = None
        self._connected = False
        self._order_id_lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return (
            self._connected
            and self._callbacks is not None
            and not self._callbacks.connection_lost.is_set()
        )

    def connect(self, *, host: str, port: int, client_id: int) -> None:
        if port != LIVE_PORT:
            raise ValueError("OfficialIbapiAdapter permits only the live IBKR port 4001")
        self.disconnect()
        callbacks = _IbkrCallbacks()
        client = self._client_factory(callbacks)
        deadline = time.monotonic() + self.connect_timeout
        connect_error: list[BaseException] = []

        def run_connect() -> None:
            try:
                client.connect(host, port, client_id)
            except BaseException as exc:  # broker SDK may throw non-Exception wrappers
                connect_error.append(exc)

        connect_thread = threading.Thread(
            target=run_connect,
            name=f"ibapi-connect-{client_id}",
            daemon=True,
        )
        self._connect_thread = connect_thread
        connect_thread.start()
        connect_thread.join(max(0.0, deadline - time.monotonic()))
        if connect_thread.is_alive():
            self._close_connection_object(client)
            connect_thread.join(self.shutdown_grace)
            self._clear_connection()
            raise BrokerConnectionTimeout("connection_timeout")
        if connect_error:
            self._clear_connection()
            raise BrokerGatewayUnavailable("connection_failed") from connect_error[0]
        if callbacks.fatal_error is not None:
            code, message = callbacks.fatal_error
            self._clear_connection()
            if code == 502:
                raise BrokerGatewayUnavailable("gateway_unavailable")
            raise BrokerGatewayUnavailable(f"ibkr_error_{code}: {message}")
        if not client.isConnected():
            self._clear_connection()
            raise BrokerGatewayUnavailable("connection_failed")

        run = getattr(client, "run", None)
        if callable(run):
            run_thread = threading.Thread(
                target=run,
                name=f"ibapi-reader-{client_id}",
                daemon=True,
            )
            self._run_thread = run_thread
            run_thread.start()
        if not callbacks.ready.wait(max(0.0, deadline - time.monotonic())):
            self._shutdown_client(client)
            self._clear_connection()
            raise BrokerConnectionTimeout("connection_timeout")
        if callbacks.fatal_error is not None:
            code, message = callbacks.fatal_error
            self._shutdown_client(client)
            self._clear_connection()
            if code == 502:
                raise BrokerGatewayUnavailable("gateway_unavailable")
            raise BrokerGatewayUnavailable(f"ibkr_error_{code}: {message}")
        self._client = client
        self._callbacks = callbacks
        self._connected = True

    def disconnect(self) -> None:
        client = self._client
        if client is not None:
            self._shutdown_client(client)
        self._clear_connection()

    def account_snapshot(self) -> BrokerAccountSnapshot:
        client, callbacks = self._require_connection()
        if not callbacks.accounts_ready.wait(self.request_timeout) or not callbacks.accounts:
            raise TimeoutError("account_snapshot_timeout")
        with callbacks.lock:
            callbacks.positions = []
            callbacks.positions_done.clear()
            callbacks.open_orders = {}
            callbacks.open_orders_done.clear()
        self._call_bounded("positions_request_send", client.reqPositions)
        if not callbacks.positions_done.wait(self.request_timeout):
            raise TimeoutError("positions_timeout")
        self._call_bounded("open_orders_request_send", client.reqAllOpenOrders)
        if not callbacks.open_orders_done.wait(self.request_timeout):
            raise TimeoutError("open_orders_timeout")
        if self.expected_account_id is None:
            if len(callbacks.accounts) != 1:
                raise BrokerGatewayUnavailable("multiple_accounts_require_selection")
            account_id = callbacks.accounts[0]
        else:
            if self.expected_account_id not in callbacks.accounts:
                raise BrokerGatewayUnavailable("configured_account_not_visible")
            account_id = self.expected_account_id
        with callbacks.lock:
            positions = tuple(
                dict(position)
                for position in callbacks.positions
                if position["account_id"] == account_id
            )
            open_orders = tuple(
                dict(order)
                for order in callbacks.open_orders.values()
                if order.get("account_id") == account_id
            )
        return BrokerAccountSnapshot(
            account_id=account_id,
            api_read_only=self.api_read_only,
            positions=positions,
            open_orders=open_orders,
        )

    def what_if(self, request: BrokerOrderRequest) -> BrokerWhatIf:
        client, callbacks = self._require_connection()
        order_id = self._reserve_order_id(callbacks)
        event = self._prepare_order_request(callbacks, order_id)
        contract, order = self._build_order(request, order_ref=f"whatif:{order_id}")
        order.orderId = order_id
        order.whatIf = True
        self._call_bounded("what_if_send", client.placeOrder, order_id, contract, order)
        if not event.wait(self.request_timeout):
            raise TimeoutError("what_if_timeout")
        error = callbacks.errors_by_request.get(order_id)
        if error is not None:
            raise RuntimeError(f"ibkr_error_{error[0]}: {error[1]}")
        with callbacks.lock:
            result = dict(callbacks.order_results[order_id])
            request_warnings = tuple(callbacks.warnings_by_request.get(order_id, ()))
        warnings = [f"{code}: {message}" for code, message in request_warnings]
        state_warning = str(result.get("warning", "")).strip()
        if state_warning:
            warnings.append(state_warning)
        return BrokerWhatIf(
            accepted=True,
            estimated_commission=self._optional_decimal(result.get("commission")),
            initial_margin_change=self._optional_decimal(result.get("initial_margin_change")),
            warning="; ".join(warnings) or None,
        )

    def submit(self, request: BrokerOrderRequest, *, order_ref: str) -> BrokerSubmission:
        client, callbacks = self._require_connection()
        if self.api_read_only:
            raise RuntimeError("IBKR API is read-only")
        order_id = self._reserve_order_id(callbacks)
        event = self._prepare_order_request(callbacks, order_id)
        contract, order = self._build_order(request, order_ref=order_ref)
        order.orderId = order_id
        self._call_bounded("order_send", client.placeOrder, order_id, contract, order)
        if not event.wait(self.request_timeout):
            raise TimeoutError("order_submission_timeout")
        error = callbacks.errors_by_request.get(order_id)
        if error is not None:
            raise BrokerOrderRejected(f"ibkr_error_{error[0]}")
        with callbacks.lock:
            result = dict(callbacks.order_results[order_id])
        return BrokerSubmission(
            status=self._normalize_status(str(result.get("status", "Submitted"))),
            order_id=order_id,
            perm_id=_IbkrCallbacks._optional_int(result.get("perm_id")),
            order_ref=order_ref,
        )

    def find_by_order_ref(self, order_ref: str) -> BrokerSubmission | None:
        client, callbacks = self._require_connection()
        with callbacks.lock:
            callbacks.open_orders = {}
            callbacks.open_orders_done.clear()
        self._call_bounded("order_recovery_send", client.reqAllOpenOrders)
        if not callbacks.open_orders_done.wait(self.request_timeout):
            raise TimeoutError("order_recovery_timeout")
        with callbacks.lock:
            matches = [
                dict(order)
                for order in callbacks.open_orders.values()
                if order.get("order_ref") == order_ref
            ]
        if not matches:
            request_completed = getattr(client, "reqCompletedOrders", None)
            if not callable(request_completed):
                return None
            with callbacks.lock:
                callbacks.completed_orders = {}
                callbacks.completed_orders_done.clear()
            self._call_bounded("completed_order_recovery_send", request_completed, True)
            if not callbacks.completed_orders_done.wait(self.request_timeout):
                raise TimeoutError("completed_order_recovery_timeout")
            with callbacks.lock:
                matches = [
                    dict(order)
                    for order in callbacks.completed_orders.values()
                    if order.get("order_ref") == order_ref
                ]
            if not matches:
                return None
        if len(matches) > 1:
            raise RuntimeError("duplicate IBKR orderRef detected")
        result = matches[0]
        return BrokerSubmission(
            status=self._normalize_status(str(result.get("status", "Submitted"))),
            order_id=int(result["order_id"]),
            perm_id=_IbkrCallbacks._optional_int(result.get("perm_id")),
            order_ref=order_ref,
        )

    def _require_connection(self) -> tuple[EClient, _IbkrCallbacks]:
        if not self.connected or self._client is None or self._callbacks is None:
            raise RuntimeError("IBKR is not connected")
        return self._client, self._callbacks

    def _call_bounded(self, name: str, function: Callable[..., Any], *args: Any) -> Any:
        result: list[Any] = []
        errors: list[BaseException] = []
        done = threading.Event()

        def invoke() -> None:
            try:
                result.append(function(*args))
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()

        thread = threading.Thread(
            target=invoke,
            name=f"ibapi-{name}",
            daemon=True,
        )
        thread.start()
        if not done.wait(self.request_timeout):
            callbacks = self._callbacks
            if callbacks is not None:
                callbacks.connection_lost.set()
            self._connected = False
            client = self._client
            if client is not None:
                closer = threading.Thread(
                    target=self._safe_disconnect,
                    args=(client,),
                    name="ibapi-timeout-disconnect",
                    daemon=True,
                )
                closer.start()
                closer.join(self.shutdown_grace)
            raise TimeoutError(f"{name}_timeout")
        if errors:
            error = errors[0]
            if isinstance(error, Exception):
                raise error
            raise RuntimeError(f"{name}_failed")
        return None if not result else result[0]

    def _reserve_order_id(self, callbacks: _IbkrCallbacks) -> int:
        with self._order_id_lock:
            if callbacks.next_order_id is None:
                raise RuntimeError("IBKR nextValidId is unavailable")
            order_id = callbacks.next_order_id
            callbacks.next_order_id += 1
            return order_id

    @staticmethod
    def _prepare_order_request(callbacks: _IbkrCallbacks, order_id: int) -> threading.Event:
        event = threading.Event()
        with callbacks.lock:
            callbacks.order_events[order_id] = event
            callbacks.order_results.pop(order_id, None)
            callbacks.errors_by_request.pop(order_id, None)
            callbacks.warnings_by_request.pop(order_id, None)
        return event

    @staticmethod
    def _build_order(request: BrokerOrderRequest, *, order_ref: str) -> tuple[Contract, Order]:
        contract = Contract()
        contract.symbol = request.symbol
        contract.secType = request.security_type
        contract.exchange = request.exchange
        contract.currency = request.currency
        order = Order()
        order.account = request.account_id
        order.action = request.side
        order.orderType = request.order_type
        order.totalQuantity = request.quantity
        order.lmtPrice = float(request.limit_price)
        order.tif = request.tif
        order.outsideRth = False
        # Modern TWS/Gateway rejects these legacy defaults with 10268/10269.
        order.eTradeOnly = False
        order.firmQuoteOnly = False
        order.transmit = True
        order.orderRef = order_ref
        return contract, order

    @staticmethod
    def _optional_decimal(value: Any) -> Decimal | None:
        if value in (None, "", "1.7976931348623157E308"):
            return None
        try:
            result = Decimal(str(value))
        except InvalidOperation:
            return None
        return result if result.is_finite() else None

    @staticmethod
    def _normalize_status(value: str) -> str:
        normalized = value.strip().lower()
        if normalized in {"presubmitted", "submitted", "pendingsubmit"}:
            return "submitted"
        if normalized == "filled":
            return "filled"
        if normalized in {"cancelled", "apicancelled"}:
            return "cancelled"
        if normalized == "inactive":
            return "rejected"
        return normalized or "unknown"

    def _clear_connection(self) -> None:
        self._client = None
        self._callbacks = None
        self._connected = False

    @staticmethod
    def _close_connection_object(client: Any) -> None:
        """Unblock a stuck ``EClient.connect`` without racing ``EClient.reset``.

        Calling ``EClient.disconnect`` while the legacy client's connect method is
        still reading the handshake can set ``conn`` to ``None`` underneath that
        thread. Closing the stable Connection object lets the connect thread own its
        own reset path.
        """

        connection = getattr(client, "conn", None)
        if connection is None:
            return
        try:
            connection.disconnect()
        except (AttributeError, OSError):
            return

    def _safe_disconnect(self, client: Any) -> None:
        try:
            client.disconnect()
        except (AttributeError, OSError):
            self._close_connection_object(client)

    def _shutdown_client(self, client: Any) -> None:
        self._safe_disconnect(client)
        run_thread = self._run_thread
        if (
            run_thread is not None
            and run_thread is not threading.current_thread()
            and run_thread.is_alive()
        ):
            run_thread.join(self.shutdown_grace)


__all__ = [
    "BrokerConnectionTimeout",
    "BrokerGatewayUnavailable",
    "OfficialIbapiAdapter",
]
