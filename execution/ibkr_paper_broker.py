"""IBKR Gateway Paper adapter for the autonomous, long-only paper executor.

The adapter deliberately owns the Paper-only seam.  It never accepts port 4001,
does not know IBKR login credentials, and converts the existing autonomous-paper
broker contract into constrained 4002 socket commands.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Protocol

from db.migrations.sqlite import SQLiteMigration, apply_sqlite_migrations
from execution.alpaca_paper import (
    BrokerError,
    BrokerOrder,
    BrokerWritesDisabledError,
    PaperAccount,
    PaperCloseRequest,
    PaperExtendedLimitRequest,
    PaperOrderRequest,
    PaperPosition,
    PaperStopRequest,
)
from execution.ibkr_execution import (
    PAPER_PORT,
    BrokerAccountSnapshot,
    BrokerOrderRejected,
    BrokerSubmission,
    BrokerWhatIf,
)
from execution.ibkr_tws_adapter import IbkrAccountValues, IbkrOrderCommand


class IBKRPaperTransport(Protocol):
    @property
    def connected(self) -> bool: ...

    def connect(self, *, host: str, port: int, client_id: int) -> None: ...

    def disconnect(self) -> None: ...

    def account_snapshot(self) -> BrokerAccountSnapshot: ...

    def account_values(self) -> IbkrAccountValues: ...

    def what_if_order_command(self, command: IbkrOrderCommand) -> BrokerWhatIf: ...

    def submit_order_group(
        self, commands: tuple[IbkrOrderCommand, ...]
    ) -> tuple[BrokerSubmission, ...]: ...

    def find_by_order_ref(self, order_ref: str) -> BrokerSubmission | None: ...

    def cancel_order(self, order_id: int) -> bool: ...


class IBKRPaperRecoveryRequired(BrokerError):
    """An order may have reached IBKR and must be reconciled before a retry."""


class IBKRPaperWhatIfBlocked(BrokerError):
    """The broker preflight returned a warning or rejected autonomous entry."""


@dataclass(frozen=True)
class _StoredOrder:
    client_order_id: str
    order_ref: str
    request_hash: str
    symbol: str
    quantity: int
    status: str
    broker_order_id: int | None


def _create_ibkr_paper_orders(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ibkr_paper_orders (
            client_order_id TEXT PRIMARY KEY,
            order_ref TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL,
            symbol TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            status TEXT NOT NULL,
            broker_order_id INTEGER,
            updated_at_utc TEXT NOT NULL
        )
        """
    )


IBKR_PAPER_MIGRATIONS = (
    SQLiteMigration(
        version=1,
        name="ibkr_paper_orders",
        signature="ibkr_paper_orders.v1",
        apply=_create_ibkr_paper_orders,
    ),
)


class IBKRPaperBroker:
    """Deep Paper-only broker module used by the existing autonomous session.

    Callers learn the same small broker interface as the previous Alpaca Paper
    adapter.  This implementation hides IBKR order-ref constraints, bracket order
    wiring, What-If gating, account conversion, and durable recovery state.
    """

    def __init__(
        self,
        *,
        path: str | Path,
        transport: IBKRPaperTransport,
        paper_account: str,
        writes_enabled: bool = False,
    ) -> None:
        normalized = paper_account.strip().upper()
        if re.fullmatch(r"DU[A-Z0-9-]{4,30}", normalized) is None:
            raise ValueError("IBKR Paper account must use a DU account identifier")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.transport = transport
        self.paper_account = normalized
        self.writes_enabled = writes_enabled
        self._initialize()

    def connect(self, *, host: str, client_id: int) -> None:
        """Connect only to the fixed IBKR Paper socket and verify the account."""

        if not host.strip():
            raise ValueError("IBKR Paper host is required")
        if isinstance(client_id, bool) or client_id < 0:
            raise ValueError("IBKR Paper client id is invalid")
        self.transport.disconnect()
        self.transport.connect(host=host.strip(), port=PAPER_PORT, client_id=client_id)
        try:
            self._snapshot_for_account()
        except Exception:
            self.transport.disconnect()
            raise

    def close(self) -> None:
        self.transport.disconnect()

    def get_account(self) -> PaperAccount:
        snapshot = self._snapshot_for_account()
        values = self.transport.account_values()
        self._assert_account(values.account_id)
        active = self.transport.connected and not snapshot.api_read_only
        return PaperAccount(
            status="ACTIVE" if active else "INACTIVE",
            account_blocked=not self.transport.connected,
            trading_blocked=not active,
            equity=_decimal_text(values.net_liquidation),
            last_equity=_decimal_text(values.previous_equity),
            buying_power=_decimal_text(values.buying_power),
        )

    def list_positions(self) -> tuple[PaperPosition, ...]:
        snapshot = self._snapshot_for_account()
        positions: list[PaperPosition] = []
        for raw in snapshot.positions:
            if str(raw.get("security_type", "")).upper() != "STK":
                continue
            symbol = str(raw.get("symbol", "")).strip().upper()
            quantity = _decimal(raw.get("quantity"), name="position quantity")
            if quantity == 0:
                continue
            average = _decimal(raw.get("average_cost"), name="position average cost")
            positions.append(
                PaperPosition(
                    symbol=symbol,
                    qty=_decimal_text(abs(quantity)),
                    side="long" if quantity > 0 else "short",
                    market_value=_decimal_text(abs(quantity) * average),
                    avg_entry_price=_decimal_text(average),
                    current_price=None,
                )
            )
        return tuple(sorted(positions, key=lambda item: item.symbol))

    def list_open_orders(self) -> tuple[BrokerOrder, ...]:
        snapshot = self._snapshot_for_account()
        orders: list[BrokerOrder] = []
        for raw in snapshot.open_orders:
            order_id = _positive_int(raw.get("order_id"), name="open order id")
            quantity = _positive_int(raw.get("quantity"), name="open order quantity")
            order_ref = str(raw.get("order_ref", ""))
            stored = self._find_by_ref(order_ref)
            client_id = (
                stored.client_order_id
                if stored is not None
                else f"manual:{order_id}"
            )
            orders.append(
                BrokerOrder(
                    id=str(order_id),
                    client_order_id=client_id,
                    symbol=str(raw.get("symbol", "")).strip().upper(),
                    qty=quantity,
                    filled_qty=str(raw.get("filled_qty", "0")),
                    status=_paper_status(str(raw.get("status", "unknown"))),
                )
            )
        return tuple(sorted(orders, key=lambda item: int(item.id)))

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        stored = self._find(client_order_id)
        if stored is None:
            return None
        recovered = self.transport.find_by_order_ref(stored.order_ref)
        if recovered is not None:
            stored = self._store_submission(stored, recovered)
            return self._broker_order(stored)
        if stored.status in {"submitting", "unknown"}:
            raise IBKRPaperRecoveryRequired("ibkr_paper_recovery_required")
        return self._broker_order(stored)

    def cancel_order(self, order_id: str) -> bool:
        self._require_writes()
        numeric = _positive_int(order_id, name="IBKR cancel order id")
        cancelled = self.transport.cancel_order(numeric)
        if cancelled:
            self._update_status_for_broker_id(numeric, "cancelled")
        return cancelled

    def submit_order_idempotent(self, request: PaperOrderRequest) -> BrokerOrder:
        self._require_writes()
        entry_type: Literal["MKT", "LMT", "STP"] = (
            "LMT" if request.order_type == "limit" else "MKT"
        )
        parent = IbkrOrderCommand(
            account_id=self.paper_account,
            symbol=request.symbol,
            side="BUY",
            quantity=request.qty,
            order_type=entry_type,
            limit_price=(
                _decimal(request.limit_price, name="entry limit price")
                if request.limit_price is not None
                else None
            ),
            order_ref=self._order_ref(request.client_order_id),
            transmit=False,
        )
        commands: list[IbkrOrderCommand] = [parent]
        child_ids: list[str] = []
        if request.take_profit_price is not None:
            target_id = f"{request.client_order_id}:take-profit"
            child_ids.append(target_id)
            commands.append(
                IbkrOrderCommand(
                    account_id=self.paper_account,
                    symbol=request.symbol,
                    side="SELL",
                    quantity=request.qty,
                    order_type="LMT",
                    limit_price=_decimal(
                        request.take_profit_price,
                        name="take-profit price",
                    ),
                    order_ref=self._order_ref(target_id),
                    parent_index=0,
                    transmit=False,
                )
            )
        stop_id = f"{request.client_order_id}:stop"
        child_ids.append(stop_id)
        commands.append(
            IbkrOrderCommand(
                account_id=self.paper_account,
                symbol=request.symbol,
                side="SELL",
                quantity=request.qty,
                order_type="STP",
                stop_price=_decimal(request.stop_loss_price, name="stop price"),
                order_ref=self._order_ref(stop_id),
                parent_index=0,
                transmit=True,
            )
        )
        return self._submit_group(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            quantity=request.qty,
            request_payload=request.model_dump(mode="json"),
            commands=tuple(commands),
            child_ids=tuple(child_ids),
        )

    def submit_close_order_idempotent(
        self, request: PaperCloseRequest
    ) -> BrokerOrder:
        self._require_writes()
        self._assert_reduces_long(request.symbol, request.qty)
        command = IbkrOrderCommand(
            account_id=self.paper_account,
            symbol=request.symbol,
            side="SELL",
            quantity=request.qty,
            order_type="MKT",
            order_ref=self._order_ref(request.client_order_id),
        )
        return self._submit_group(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            quantity=request.qty,
            request_payload=request.model_dump(mode="json"),
            commands=(command,),
            child_ids=(),
        )

    def submit_stop_order_idempotent(self, request: PaperStopRequest) -> BrokerOrder:
        self._require_writes()
        self._assert_reduces_long(request.symbol, request.qty)
        command = IbkrOrderCommand(
            account_id=self.paper_account,
            symbol=request.symbol,
            side="SELL",
            quantity=request.qty,
            order_type="STP",
            stop_price=_decimal(request.stop_price, name="stop price"),
            order_ref=self._order_ref(request.client_order_id),
        )
        return self._submit_group(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            quantity=request.qty,
            request_payload=request.model_dump(mode="json"),
            commands=(command,),
            child_ids=(),
        )

    def submit_extended_limit_idempotent(
        self, request: PaperExtendedLimitRequest
    ) -> BrokerOrder:
        self._require_writes()
        if request.side == "sell":
            self._assert_reduces_long(request.symbol, request.qty)
        command = IbkrOrderCommand(
            account_id=self.paper_account,
            symbol=request.symbol,
            side=request.side.upper(),  # type: ignore[arg-type]
            quantity=request.qty,
            order_type="LMT",
            limit_price=_decimal(request.limit_price, name="extended limit price"),
            outside_rth=True,
            order_ref=self._order_ref(request.client_order_id),
        )
        return self._submit_group(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            quantity=request.qty,
            request_payload=request.model_dump(mode="json"),
            commands=(command,),
            child_ids=(),
        )

    def _submit_group(
        self,
        *,
        client_order_id: str,
        symbol: str,
        quantity: int,
        request_payload: dict[str, object],
        commands: tuple[IbkrOrderCommand, ...],
        child_ids: tuple[str, ...],
    ) -> BrokerOrder:
        fingerprint = _request_hash(request_payload)
        root_ref = commands[0].order_ref
        existing = self._ensure(
            client_order_id=client_order_id,
            order_ref=root_ref,
            request_hash=fingerprint,
            symbol=symbol,
            quantity=quantity,
        )
        if not existing[1]:
            return self._recover_or_return(existing[0])
        children: list[_StoredOrder] = []
        for child_id, command in zip(child_ids, commands[1:], strict=True):
            child, created = self._ensure(
                client_order_id=child_id,
                order_ref=command.order_ref,
                request_hash=fingerprint,
                symbol=symbol,
                quantity=quantity,
            )
            if not created:
                raise IBKRPaperRecoveryRequired("ibkr_paper_child_recovery_required")
            children.append(child)
        try:
            what_if = self.transport.what_if_order_command(commands[0])
            if not what_if.accepted or what_if.warning:
                self._update_status(existing[0].client_order_id, "rejected")
                for child in children:
                    self._update_status(child.client_order_id, "rejected")
                raise IBKRPaperWhatIfBlocked("what_if_warning")
            submissions = self.transport.submit_order_group(commands)
        except IBKRPaperWhatIfBlocked:
            raise
        except BrokerOrderRejected:
            self._update_status(existing[0].client_order_id, "rejected")
            for child in children:
                self._update_status(child.client_order_id, "rejected")
            raise
        except RuntimeError:
            self._update_status(existing[0].client_order_id, "unknown")
            for child in children:
                self._update_status(child.client_order_id, "unknown")
            raise
        except Exception as exc:
            self._update_status(existing[0].client_order_id, "unknown")
            for child in children:
                self._update_status(child.client_order_id, "unknown")
            raise IBKRPaperRecoveryRequired("ibkr_paper_recovery_required") from exc
        if len(submissions) != len(commands):
            self._update_status(existing[0].client_order_id, "unknown")
            raise IBKRPaperRecoveryRequired("ibkr_paper_recovery_required")
        root = self._store_submission(existing[0], submissions[0])
        for child, submission in zip(children, submissions[1:], strict=True):
            self._store_submission(child, submission)
        return self._broker_order(root)

    def _assert_reduces_long(self, symbol: str, quantity: int) -> None:
        current = sum(
            _positive_int(position.qty, name="position quantity")
            for position in self.list_positions()
            if position.symbol == symbol and position.side == "long"
        )
        active_sells = sum(
            order.qty
            for order in self.list_open_orders()
            if order.symbol == symbol
            and order.status in {"new", "accepted", "pending_new", "partially_filled"}
            and self._order_side(order.id) == "SELL"
        )
        if quantity > current - active_sells:
            raise RuntimeError("IBKR Paper reduce order exceeds the available long position")

    def _order_side(self, order_id: str) -> str:
        snapshot = self._snapshot_for_account()
        for order in snapshot.open_orders:
            if str(order.get("order_id")) == order_id:
                return str(order.get("side", "")).upper()
        return ""

    def _snapshot_for_account(self) -> BrokerAccountSnapshot:
        if not self.transport.connected:
            raise BrokerError("IBKR Paper is not connected")
        snapshot = self.transport.account_snapshot()
        self._assert_account(snapshot.account_id)
        return snapshot

    def _assert_account(self, account_id: str) -> None:
        if account_id.strip().upper() != self.paper_account:
            raise BrokerError("IBKR Paper account mismatch")

    def _require_writes(self) -> None:
        if not self.writes_enabled:
            raise BrokerWritesDisabledError("IBKR Paper writes are disabled")
        account = self.get_account()
        if account.trading_blocked or account.account_blocked:
            raise BrokerError("IBKR Paper trading is unavailable")

    def _initialize(self) -> None:
        with self._connect() as connection:
            apply_sqlite_migrations(
                connection,
                owner="execution.ibkr_paper_broker",
                migrations=IBKR_PAPER_MIGRATIONS,
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _ensure(
        self,
        *,
        client_order_id: str,
        order_ref: str,
        request_hash: str,
        symbol: str,
        quantity: int,
    ) -> tuple[_StoredOrder, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM ibkr_paper_orders WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO ibkr_paper_orders (
                        client_order_id, order_ref, request_hash, symbol, quantity,
                        status, broker_order_id, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, 'submitting', NULL, datetime('now'))
                    """,
                    (client_order_id, order_ref, request_hash, symbol, quantity),
                )
                row = connection.execute(
                    "SELECT * FROM ibkr_paper_orders WHERE client_order_id=?",
                    (client_order_id,),
                ).fetchone()
                connection.commit()
                return _stored(row), True
            connection.commit()
        existing = _stored(row)
        if (
            existing.order_ref != order_ref
            or existing.request_hash != request_hash
            or existing.symbol != symbol
            or existing.quantity != quantity
        ):
            raise RuntimeError("IBKR Paper client order id identity conflict")
        return existing, False

    def _find(self, client_order_id: str) -> _StoredOrder | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ibkr_paper_orders WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
        return None if row is None else _stored(row)

    def _find_by_ref(self, order_ref: str) -> _StoredOrder | None:
        if not order_ref:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ibkr_paper_orders WHERE order_ref=?",
                (order_ref,),
            ).fetchone()
        return None if row is None else _stored(row)

    def _recover_or_return(self, stored: _StoredOrder) -> BrokerOrder:
        if stored.status in {"submitting", "unknown"}:
            recovered = self.transport.find_by_order_ref(stored.order_ref)
            if recovered is None:
                raise IBKRPaperRecoveryRequired("ibkr_paper_recovery_required")
            stored = self._store_submission(stored, recovered)
        if stored.status == "rejected":
            raise RuntimeError("IBKR Paper order was rejected")
        return self._broker_order(stored)

    def _store_submission(
        self,
        stored: _StoredOrder,
        submission: BrokerSubmission,
    ) -> _StoredOrder:
        status = _paper_status(submission.status)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ibkr_paper_orders
                SET status=?, broker_order_id=?, updated_at_utc=datetime('now')
                WHERE client_order_id=?
                """,
                (status, submission.order_id, stored.client_order_id),
            )
        return _StoredOrder(
            **{
                **stored.__dict__,
                "status": status,
                "broker_order_id": submission.order_id,
            }
        )

    def _update_status(self, client_order_id: str, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ibkr_paper_orders
                SET status=?, updated_at_utc=datetime('now')
                WHERE client_order_id=?
                """,
                (status, client_order_id),
            )

    def _update_status_for_broker_id(self, broker_order_id: int, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ibkr_paper_orders
                SET status=?, updated_at_utc=datetime('now')
                WHERE broker_order_id=?
                """,
                (status, broker_order_id),
            )

    @staticmethod
    def _order_ref(client_order_id: str) -> str:
        digest = hashlib.sha256(client_order_id.encode("utf-8")).hexdigest()[:32]
        return f"aqp:{digest}"

    @staticmethod
    def _broker_order(stored: _StoredOrder) -> BrokerOrder:
        if stored.broker_order_id is None:
            raise IBKRPaperRecoveryRequired("IBKR Paper order has no broker id")
        return BrokerOrder(
            id=str(stored.broker_order_id),
            client_order_id=stored.client_order_id,
            symbol=stored.symbol,
            qty=stored.quantity,
            filled_qty="0",
            status=_paper_status(stored.status),
        )


def _stored(row: sqlite3.Row | None) -> _StoredOrder:
    if row is None:
        raise RuntimeError("IBKR Paper order record disappeared")
    return _StoredOrder(
        client_order_id=str(row["client_order_id"]),
        order_ref=str(row["order_ref"]),
        request_hash=str(row["request_hash"]),
        symbol=str(row["symbol"]),
        quantity=int(row["quantity"]),
        status=str(row["status"]),
        broker_order_id=(
            None if row["broker_order_id"] is None else int(row["broker_order_id"])
        ),
    )


def _request_hash(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _decimal(value: object, *, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"IBKR Paper {name} is invalid") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"IBKR Paper {name} is invalid")
    return result


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _positive_int(value: object, *, name: str) -> int:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"IBKR Paper {name} is invalid") from exc
    if decimal <= 0 or decimal != decimal.to_integral_value():
        raise ValueError(f"IBKR Paper {name} is invalid")
    return int(decimal)


def _paper_status(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"submitted", "presubmitted", "pendingsubmit"}:
        return "accepted"
    if normalized in {"filled", "cancelled", "canceled", "rejected", "inactive"}:
        return normalized if normalized != "inactive" else "rejected"
    return normalized or "unknown"


__all__ = [
    "IBKRPaperBroker",
    "IBKRPaperRecoveryRequired",
    "IBKRPaperTransport",
    "IBKRPaperWhatIfBlocked",
]
