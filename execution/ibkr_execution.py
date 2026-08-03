"""Fail-closed Interactive Brokers execution desk.

The desk talks only to an already-authenticated local TWS or IB Gateway session.
Credentials are deliberately outside this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

LIVE_PORT = 4001
SCHEMA_VERSION = "ibkr.execution.v1"


@dataclass(frozen=True)
class BrokerAccountSnapshot:
    account_id: str
    api_read_only: bool
    positions: tuple[Mapping[str, Any], ...]
    open_orders: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class BrokerOrderRequest:
    client_order_id: str
    account_id: str
    symbol: str
    security_type: str
    exchange: str
    currency: str
    order_type: str
    tif: str
    side: str
    operation: str
    quantity: int
    limit_price: Decimal


@dataclass(frozen=True)
class BrokerWhatIf:
    accepted: bool
    estimated_commission: Decimal | None
    initial_margin_change: Decimal | None
    warning: str | None


@dataclass(frozen=True)
class BrokerSubmission:
    status: str
    order_id: int
    perm_id: int | None
    order_ref: str


class BrokerOrderRejected(RuntimeError):
    """The broker definitively rejected an order without accepting it."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BrokerPort(Protocol):
    @property
    def connected(self) -> bool: ...

    def connect(self, *, host: str, port: int, client_id: int) -> None: ...

    def disconnect(self) -> None: ...

    def account_snapshot(self) -> BrokerAccountSnapshot: ...

    def what_if(self, request: BrokerOrderRequest) -> BrokerWhatIf: ...

    def submit(self, request: BrokerOrderRequest, *, order_ref: str) -> BrokerSubmission: ...

    def find_by_order_ref(self, order_ref: str) -> BrokerSubmission | None: ...


def _mask_account(account_id: str | None) -> str | None:
    if not account_id:
        return None
    if len(account_id) <= 4:
        return "*" * len(account_id)
    return f"{account_id[0]}{'*' * (len(account_id) - 5)}{account_id[-4:]}"


class ExecutionDesk:
    """Stateful command seam used by desktop IPC and tests."""

    def __init__(
        self,
        path: str | Path,
        broker: BrokerPort,
        *,
        paper_account: str | None = None,
        live_account: str | None = None,
        host: str = "127.0.0.1",
        client_id: int = 71,
        max_notional: Decimal = Decimal("10000"),
        preview_ttl: timedelta = timedelta(seconds=30),
        arm_ttl: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        normalized_account = "" if live_account is None else live_account.strip()
        normalized_notional = Decimal(str(max_notional))
        if not normalized_notional.is_finite() or normalized_notional <= 0:
            raise ValueError("max_notional must be positive and finite")
        if preview_ttl <= timedelta(0) or preview_ttl > timedelta(seconds=60):
            raise ValueError("preview_ttl must be between 0 and 60 seconds")
        if arm_ttl <= timedelta(0) or arm_ttl > timedelta(minutes=5):
            raise ValueError("arm_ttl must be between 0 and 5 minutes")
        if isinstance(client_id, bool) or not isinstance(client_id, int) or client_id < 0:
            raise ValueError("client_id must be a non-negative integer")
        if not host.strip():
            raise ValueError("IBKR host is required")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.broker = broker
        del paper_account  # legacy constructor compatibility; execution is live-only
        self.live_account = normalized_account or None
        self.host = host.strip()
        self.client_id = client_id
        self.max_notional = normalized_notional
        self.preview_ttl = preview_ttl
        self.arm_ttl = arm_ttl
        self.clock = clock or (lambda: datetime.now(UTC))
        self.token_factory = token_factory or (lambda: secrets.token_hex(4).upper())
        self.mode = "live"
        self.connected = False
        self.writes_armed = False
        self.armed_until_utc: datetime | None = None
        self.account: BrokerAccountSnapshot | None = None
        self.account_refreshed_at_utc: datetime | None = None
        self.last_error: str | None = None
        self.preview: dict[str, Any] | None = None
        self._lock = threading.RLock()
        self._initialize_ledger()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        self._sync_broker_health()
        self._expire_arm()
        account = self.account
        masked = _mask_account(None if account is None else account.account_id)
        account_bound = self._account_bound()
        arm_phrase = None
        binding_phrase = None
        if self.connected and account is not None and account_bound:
            arm_phrase = f"启用实盘下单 {masked}"
        elif self.connected and account is not None:
            binding_phrase = f"绑定实盘账户 {masked}"
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "execution_snapshot",
            "mode": self.mode,
            "port": LIVE_PORT,
            "max_order_notional": format(self.max_notional, "f"),
            "max_opening_notional": format(self.max_notional, "f"),
            "notional_cap_scope": "OpenLong_only",
            "enabled": self.connected,
            "connected": self.connected,
            "account_masked": masked,
            "account_bound": account_bound,
            "api_read_only": True if account is None else account.api_read_only,
            "writes_armed": self.writes_armed,
            "armed_until_utc": (
                None if self.armed_until_utc is None else self.armed_until_utc.isoformat()
            ),
            "positions": [] if account is None else list(account.positions),
            "open_orders": [] if account is None else list(account.open_orders),
            "account_snapshot_stale": account is not None and not self.connected,
            "orders_left_working": self._active_orders_count(),
            "recovery_required": self._recovery_required(),
            "last_error": self.last_error,
            "arm_confirmation_phrase": arm_phrase,
            "binding_confirmation_phrase": binding_phrase,
            "account_refreshed_at_utc": (
                None
                if self.account_refreshed_at_utc is None
                else self.account_refreshed_at_utc.isoformat()
            ),
            "recent_orders": self._recent_orders(),
        }

    def handle(self, command: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._handle(command)

    def _handle(self, command: Mapping[str, Any]) -> dict[str, Any]:
        self._sync_broker_health()
        self._expire_arm()
        command_kind = command.get("kind", command.get("type"))
        if command_kind == "snapshot":
            return self.snapshot()
        if command_kind == "arm":
            return self._arm(command)
        if command_kind == "bind_account":
            return self._bind_account(command)
        if command_kind == "disarm":
            self._disarm()
            return self.snapshot()
        if command_kind == "preview":
            return self._preview(command)
        if command_kind == "submit":
            return self._submit(command)
        if command_kind == "recover":
            return self._recover()
        if command_kind == "disconnect":
            return self._disconnect()
        if command_kind == "switch":
            if command.get("mode") != "live":
                raise ValueError("execution is live-only; paper switching is not supported")
            command_kind = "connect"
        if command_kind != "connect":
            raise KeyError(f"unknown IBKR execution command: {command_kind}")
        return self._connect_live()

    def _connect_live(self) -> dict[str, Any]:
        self.broker.disconnect()
        self.mode = "live"
        self.connected = False
        self._disarm()
        self.account = None
        self.account_refreshed_at_utc = None
        self.preview = None
        self.last_error = None
        try:
            self.broker.connect(host=self.host, port=LIVE_PORT, client_id=self.client_id)
            account = self.broker.account_snapshot()
        except Exception as exc:
            self.broker.disconnect()
            self.last_error = str(exc) or "connection_failed"
            raise
        expected = self.live_account
        if expected is not None and account.account_id != expected:
            self.broker.disconnect()
            self.last_error = "account_mismatch"
            raise RuntimeError("connected IBKR account does not match configured mode account")
        self.account = account
        self.account_refreshed_at_utc = self._utc_now()
        self.connected = True
        if self._account_bound():
            self._recover_uncertain_orders()
        return self.snapshot()

    def _disconnect(self) -> dict[str, Any]:
        self.broker.disconnect()
        self.connected = False
        self._disarm()
        self.preview = None
        self.last_error = None
        return self.snapshot()

    def _arm(self, command: Mapping[str, Any]) -> dict[str, Any]:
        if not self.connected or self.account is None:
            raise RuntimeError("IBKR is not connected")
        if not self._account_bound():
            raise RuntimeError("IBKR account is not bound")
        if self.account.api_read_only:
            raise RuntimeError("IBKR API is read-only")
        masked = _mask_account(self.account.account_id)
        expected = f"启用实盘下单 {masked}"
        if command.get("confirmation") != expected:
            raise RuntimeError("arm confirmation does not match")
        self.writes_armed = True
        self.armed_until_utc = self._utc_now() + self.arm_ttl
        return self.snapshot()

    def _bind_account(self, command: Mapping[str, Any]) -> dict[str, Any]:
        if not self.connected or self.account is None:
            raise RuntimeError("IBKR is not connected")
        if self._account_bound():
            raise RuntimeError("IBKR account is already bound")
        masked = _mask_account(self.account.account_id)
        expected = f"绑定实盘账户 {masked}"
        if command.get("confirmation") != expected:
            raise RuntimeError("account binding confirmation does not match")
        self.live_account = self.account.account_id
        self._disarm()
        self.preview = None
        self._recover_uncertain_orders()
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "account_binding_receipt",
            "actual_account_id": self.account.account_id,
            "account_masked": masked,
            "account_bound": True,
        }

    def _preview(self, command: Mapping[str, Any]) -> dict[str, Any]:
        if not self.connected or self.account is None:
            raise RuntimeError("IBKR is not connected")
        if not self._account_bound():
            raise RuntimeError("IBKR account is not bound")
        if self._recovery_required():
            raise RuntimeError("recovery_required: resolve uncertain IBKR orders first")
        self._refresh_account()
        raw_order = command.get("order")
        if not isinstance(raw_order, Mapping):
            raise ValueError("preview order is required")
        request, intent = self._normalize_order(raw_order)
        what_if = self.broker.what_if(request)
        if not what_if.accepted:
            raise RuntimeError(what_if.warning or "IBKR what-if rejected the order")
        now = self._utc_now()
        token = self.token_factory()
        preview_id = f"preview-{token}"
        masked = _mask_account(self.account.account_id)
        warning_hash = (
            None
            if not what_if.warning
            else hashlib.sha256(what_if.warning.encode("utf-8")).hexdigest()[:8].upper()
        )
        phrase = (
            f"确认实盘下单 {masked} {request.operation} "
            f"{request.quantity} {request.symbol} @{request.limit_price:.2f} {token}"
        )
        if warning_hash is not None:
            phrase = f"{phrase} 警告{warning_hash}"
        expires = now + self.preview_ttl
        payload = {
            "schema_version": SCHEMA_VERSION,
            "kind": "execution_preview",
            "status": "previewed",
            "preview_id": preview_id,
            "mode": self.mode,
            "account_masked": masked,
            "intent": intent,
            "what_if": {
                "accepted": what_if.accepted,
                "estimated_commission": self._decimal_text(what_if.estimated_commission),
                "initial_margin_change": self._decimal_text(what_if.initial_margin_change),
                "warning": what_if.warning,
            },
            "confirmation_phrase": phrase,
            "warning_confirmation_hash": warning_hash,
            "expires_at_utc": expires.isoformat(),
        }
        self.preview = {
            **payload,
            "request": request,
            "expires_at": expires,
        }
        return payload

    def _submit(self, command: Mapping[str, Any]) -> dict[str, Any]:
        if not self.connected or self.account is None:
            raise RuntimeError("IBKR is not connected")
        if not self._account_bound():
            raise RuntimeError("IBKR account is not bound")
        if self._recovery_required():
            raise RuntimeError("recovery_required: resolve uncertain IBKR orders first")
        if not self.writes_armed:
            raise RuntimeError("IBKR writes are not armed")
        preview = self.preview
        if preview is None or command.get("preview_id") != preview["preview_id"]:
            raise RuntimeError("a matching preview is required")
        if preview["mode"] != self.mode or preview["account_masked"] != _mask_account(
            self.account.account_id
        ):
            raise RuntimeError("preview no longer matches the selected mode and account")
        if self._utc_now() > preview["expires_at"]:
            self.preview = None
            raise RuntimeError("preview expired")
        if command.get("confirmation") != preview["confirmation_phrase"]:
            raise RuntimeError("live order confirmation does not match")
        raw_order = command.get("order")
        if not isinstance(raw_order, Mapping):
            raise RuntimeError("live order must exactly match its preview")
        self._refresh_account()
        _request, intent = self._normalize_order(raw_order)
        if intent != preview["intent"]:
            raise RuntimeError("live order must exactly match its preview")
        request = preview["request"]
        refreshed_what_if = self.broker.what_if(request)
        if not refreshed_what_if.accepted:
            raise RuntimeError(
                refreshed_what_if.warning or "IBKR what-if rejected the order"
            )
        if refreshed_what_if.warning != preview["what_if"]["warning"]:
            raise RuntimeError("IBKR what-if warning changed; create a new preview")
        order_ref = (
            f"vq:{self.mode}:{self.account.account_id}:{self.client_id}:{request.client_order_id}"
        )
        intent_json = json.dumps(
            preview["intent"], sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        existing = self._create_or_read_order(
            request=request,
            intent_json=intent_json,
            order_ref=order_ref,
        )
        if str(existing["intent_json"]) != intent_json:
            raise RuntimeError("client_order_id already belongs to a different intent")
        if str(existing["status"]) not in {"submitting", "unknown"}:
            self._disarm()
            self.preview = None
            return self._refresh_after_receipt(self._stored_order_response(existing))
        if str(existing["status"]) in {"submitting", "unknown"} and not bool(
            existing["created_now"]
        ):
            return self._recover_uncertain_order(existing)
        self._disarm()
        try:
            submission = self.broker.submit(request, order_ref=order_ref)
        except BrokerOrderRejected as exc:
            self._update_order(
                order_ref,
                status="rejected",
                broker_order_id=None,
                perm_id=None,
                last_error=self._safe_error_code(exc.code, fallback="broker_rejected"),
            )
            self.preview = None
            return self._stored_order_response(self._read_order(order_ref))
        except TimeoutError:
            self._update_order(
                order_ref,
                status="unknown",
                broker_order_id=None,
                perm_id=None,
                last_error="broker_timeout",
            )
            self.preview = None
            return self._stored_order_response(self._read_order(order_ref))
        except Exception:
            self._update_order(
                order_ref,
                status="unknown",
                broker_order_id=None,
                perm_id=None,
                last_error="broker_exception",
            )
            self.preview = None
            return self._stored_order_response(self._read_order(order_ref))
        self._update_order(
            order_ref,
            status=submission.status,
            broker_order_id=submission.order_id,
            perm_id=submission.perm_id,
            last_error=None,
        )
        self.preview = None
        return self._refresh_after_receipt(
            self._stored_order_response(self._read_order(order_ref))
        )

    def _refresh_after_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        try:
            self._refresh_account()
        except Exception:
            self.last_error = "account_refresh_timeout"
            receipt["post_submit_snapshot_refreshed"] = False
            receipt["snapshot_refresh_error"] = "account_refresh_timeout"
        else:
            receipt["post_submit_snapshot_refreshed"] = True
            receipt["snapshot_refresh_error"] = None
        receipt["orders_left_working"] = self._active_orders_count()
        return receipt

    def _normalize_order(
        self, raw_order: Mapping[str, Any]
    ) -> tuple[BrokerOrderRequest, dict[str, Any]]:
        account = self.account
        if account is None:
            raise RuntimeError("IBKR is not connected")
        required = {
            "client_order_id",
            "symbol",
            "security_type",
            "exchange",
            "currency",
            "order_type",
            "tif",
            "action",
            "quantity",
            "limit_price",
        }
        missing = required - raw_order.keys()
        extras = raw_order.keys() - (required | {"max_notional"})
        if missing:
            raise ValueError(f"missing order fields: {', '.join(sorted(missing))}")
        if extras:
            raise ValueError(f"unknown order fields: {', '.join(sorted(extras))}")
        symbol = str(raw_order["symbol"]).strip().upper()
        if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", symbol) is None:
            raise ValueError("symbol must be a US stock symbol")
        fixed_values = {
            "security_type": "STK",
            "exchange": "SMART",
            "currency": "USD",
            "order_type": "LMT",
            "tif": "DAY",
        }
        normalized: dict[str, str] = {}
        for field, expected in fixed_values.items():
            value = str(raw_order[field]).strip().upper()
            if value != expected:
                raise ValueError(f"{field} must be {expected}")
            normalized[field] = value
        operation = str(raw_order["action"])
        if operation not in {"OpenLong", "ReduceLong"}:
            raise ValueError("action must be OpenLong or ReduceLong")
        quantity = raw_order["quantity"]
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        try:
            price = Decimal(str(raw_order["limit_price"]))
        except InvalidOperation as exc:
            raise ValueError("limit_price must be numeric") from exc
        if not price.is_finite() or price <= 0:
            raise ValueError("limit_price must be positive and finite")
        price = price.quantize(Decimal("0.01"))
        notional = price * quantity
        command_cap = raw_order.get("max_notional", self.max_notional)
        try:
            cap = min(self.max_notional, Decimal(str(command_cap)))
        except InvalidOperation as exc:
            raise ValueError("max_notional must be numeric") from exc
        if cap <= 0 or (operation == "OpenLong" and notional > cap):
            raise ValueError("order exceeds max_notional")
        client_order_id = str(raw_order["client_order_id"]).strip()
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", client_order_id) is None:
            raise ValueError(
                "client_order_id must contain only letters, digits, dot, dash, or underscore"
            )
        request = BrokerOrderRequest(
            client_order_id=client_order_id,
            account_id=account.account_id,
            symbol=symbol,
            security_type=normalized["security_type"],
            exchange=normalized["exchange"],
            currency=normalized["currency"],
            order_type=normalized["order_type"],
            tif=normalized["tif"],
            side="BUY" if operation == "OpenLong" else "SELL",
            operation=operation,
            quantity=quantity,
            limit_price=price,
        )
        if operation == "OpenLong" and self._active_open_quantity(symbol, "BUY") > 0:
            raise ValueError("an active BUY order already exists for this symbol")
        if operation == "ReduceLong":
            available = self._long_position_quantity(symbol) - self._active_open_quantity(
                symbol, "SELL"
            )
            if Decimal(quantity) > available:
                raise ValueError("ReduceLong quantity exceeds the current long position")
        intent = {
            "client_order_id": client_order_id,
            "symbol": symbol,
            **normalized,
            "action": operation,
            "quantity": quantity,
            "limit_price": f"{price:.2f}",
            "notional": f"{notional:.2f}",
        }
        return request, intent

    def _long_position_quantity(self, symbol: str) -> Decimal:
        if self.account is None:
            return Decimal(0)
        total = Decimal(0)
        for position in self.account.positions:
            if str(position.get("symbol", "")).strip().upper() != symbol:
                continue
            raw_quantity = position.get("quantity", position.get("position", 0))
            try:
                quantity = Decimal(str(raw_quantity))
            except InvalidOperation:
                continue
            if quantity > 0:
                total += quantity
        return total

    def _active_open_quantity(self, symbol: str, side: str) -> Decimal:
        if self.account is None:
            return Decimal(0)
        terminal = {"filled", "cancelled", "canceled", "apicancelled", "rejected", "inactive"}
        total = Decimal(0)
        for order in self.account.open_orders:
            if str(order.get("symbol", "")).strip().upper() != symbol:
                continue
            order_side = str(order.get("side", order.get("action", ""))).strip().upper()
            if order_side != side:
                continue
            if str(order.get("status", "")).strip().lower() in terminal:
                continue
            raw_quantity = order.get("remaining", order.get("quantity", 0))
            try:
                quantity = Decimal(str(raw_quantity))
            except InvalidOperation:
                continue
            if quantity > 0:
                total += quantity
        return total

    def _active_orders_count(self) -> int:
        if self.account is None:
            return 0
        terminal = {
            "filled",
            "cancelled",
            "canceled",
            "apicancelled",
            "rejected",
            "inactive",
        }
        return sum(
            1
            for order in self.account.open_orders
            if str(order.get("status", "")).strip().lower() not in terminal
        )

    def _account_bound(self) -> bool:
        return (
            self.account is not None
            and self.live_account is not None
            and self.account.account_id == self.live_account
        )

    def _refresh_account(self) -> BrokerAccountSnapshot:
        if not self.connected:
            raise RuntimeError("IBKR is not connected")
        try:
            account = self.broker.account_snapshot()
        except Exception:
            self.last_error = "account_refresh_timeout"
            raise
        if self.live_account is not None and account.account_id != self.live_account:
            self._disarm()
            self.preview = None
            self.account = None
            self.account_refreshed_at_utc = None
            self.connected = False
            self.last_error = "account_mismatch"
            self.broker.disconnect()
            raise RuntimeError("connected IBKR account does not match bound live account")
        self.account = account
        self.account_refreshed_at_utc = self._utc_now()
        self.last_error = None
        return account

    def _disarm(self) -> None:
        self.writes_armed = False
        self.armed_until_utc = None

    def _expire_arm(self) -> None:
        if self.armed_until_utc is not None and self._utc_now() >= self.armed_until_utc:
            self._disarm()

    def _sync_broker_health(self) -> None:
        if not self.connected:
            return
        if bool(getattr(self.broker, "connected", True)):
            return
        self.connected = False
        self._disarm()
        self.preview = None
        self.last_error = "connection_lost"

    @staticmethod
    def _safe_error_code(value: str, *, fallback: str) -> str:
        normalized = value.strip()
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", normalized) is None:
            return fallback
        return normalized

    def _utc_now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("execution clock must return timezone-aware UTC")
        return value

    @staticmethod
    def _decimal_text(value: Decimal | None) -> str | None:
        return None if value is None else format(value, "f")

    def _connect_ledger(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize_ledger(self) -> None:
        with self._connect_ledger() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ibkr_execution_orders (
                    idempotency_key TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    client_id INTEGER NOT NULL,
                    client_order_id TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    broker_order_id INTEGER,
                    perm_id INTEGER,
                    order_ref TEXT NOT NULL UNIQUE,
                    last_error TEXT,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
                """
            )

    def _create_or_read_order(
        self,
        *,
        request: BrokerOrderRequest,
        intent_json: str,
        order_ref: str,
    ) -> dict[str, Any]:
        key = f"{self.mode}:{request.account_id}:{self.client_id}:{request.client_order_id}"
        now = self._utc_now().isoformat()
        with self._connect_ledger() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM ibkr_execution_orders WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                connection.commit()
                return {**dict(row), "created_now": False}
            connection.execute(
                """
                INSERT INTO ibkr_execution_orders (
                    idempotency_key, mode, account_id, client_id, client_order_id,
                    intent_json, status, broker_order_id, perm_id, order_ref,
                    last_error, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, 'submitting', NULL, NULL, ?, NULL, ?, ?)
                """,
                (
                    key,
                    self.mode,
                    request.account_id,
                    self.client_id,
                    request.client_order_id,
                    intent_json,
                    order_ref,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM ibkr_execution_orders WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            connection.commit()
        assert row is not None
        return {**dict(row), "created_now": True}

    def _read_order(self, order_ref: str) -> dict[str, Any]:
        with self._connect_ledger() as connection:
            row = connection.execute(
                "SELECT * FROM ibkr_execution_orders WHERE order_ref = ?", (order_ref,)
            ).fetchone()
        if row is None:
            raise RuntimeError("IBKR order ledger entry disappeared")
        return dict(row)

    def _update_order(
        self,
        order_ref: str,
        *,
        status: str,
        broker_order_id: int | None,
        perm_id: int | None,
        last_error: str | None,
    ) -> None:
        with self._connect_ledger() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE ibkr_execution_orders
                SET status = ?, broker_order_id = ?, perm_id = ?, last_error = ?,
                    updated_at_utc = ?
                WHERE order_ref = ?
                """,
                (
                    status,
                    broker_order_id,
                    perm_id,
                    last_error,
                    self._utc_now().isoformat(),
                    order_ref,
                ),
            )
            connection.commit()

    def _recover_uncertain_order(self, stored: Mapping[str, Any]) -> dict[str, Any]:
        order_ref = str(stored["order_ref"])
        try:
            found = self.broker.find_by_order_ref(order_ref)
        except TimeoutError:
            found = None
        if found is not None:
            self._update_order(
                order_ref,
                status=found.status,
                broker_order_id=found.order_id,
                perm_id=found.perm_id,
                last_error=None,
            )
        elif str(stored["status"]) == "submitting":
            self._update_order(
                order_ref,
                status="unknown",
                broker_order_id=None,
                perm_id=None,
                last_error="recovery_lookup_empty",
            )
        self.preview = None
        return self._stored_order_response(self._read_order(order_ref))

    def _recover_uncertain_orders(self) -> None:
        account = self.account
        if account is None:
            return
        with self._connect_ledger() as connection:
            rows = connection.execute(
                """
                SELECT * FROM ibkr_execution_orders
                WHERE account_id = ? AND client_id = ?
                  AND status IN ('submitting', 'unknown')
                ORDER BY created_at_utc, client_order_id
                """,
                (account.account_id, self.client_id),
            ).fetchall()
        for row in rows:
            self._recover_uncertain_order(dict(row))

    def _recover(self) -> dict[str, Any]:
        if not self.connected or self.account is None:
            raise RuntimeError("IBKR is not connected")
        if not self._account_bound():
            raise RuntimeError("IBKR account is not bound")
        self._disarm()
        self.preview = None
        self._recover_uncertain_orders()
        return self.snapshot()

    def _stored_order_response(self, stored: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "execution_receipt",
            "status": str(stored["status"]),
            "client_order_id": str(stored["client_order_id"]),
            "broker_order_id": stored["broker_order_id"],
            "perm_id": stored["perm_id"],
            "order_ref": str(stored["order_ref"]),
            "mode": str(stored["mode"]),
            "account_masked": _mask_account(str(stored["account_id"])),
            "last_error_code": stored.get("last_error"),
            "orders_left_working": self._active_orders_count(),
        }

    def _recent_orders(self) -> list[dict[str, Any]]:
        with self._connect_ledger() as connection:
            rows = connection.execute(
                """
                SELECT client_order_id, broker_order_id, perm_id, account_id,
                       intent_json, status, updated_at_utc
                FROM ibkr_execution_orders
                ORDER BY updated_at_utc DESC, client_order_id DESC
                LIMIT 50
                """
            ).fetchall()
        recent: list[dict[str, Any]] = []
        for row in rows:
            try:
                intent = json.loads(str(row["intent_json"]))
            except (json.JSONDecodeError, TypeError):
                continue
            recent.append(
                {
                    "client_order_id": str(row["client_order_id"]),
                    "broker_order_id": row["broker_order_id"],
                    "perm_id": row["perm_id"],
                    "account_masked": _mask_account(str(row["account_id"])),
                    "symbol": str(intent.get("symbol", "")),
                    "action": str(intent.get("action", "")),
                    "quantity": intent.get("quantity"),
                    "limit_price": str(intent.get("limit_price", "")),
                    "status": str(row["status"]),
                    "updated_at_utc": str(row["updated_at_utc"]),
                }
            )
        return recent

    def _recovery_required(self) -> bool:
        with self._connect_ledger() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM ibkr_execution_orders
                WHERE status IN ('submitting', 'unknown') LIMIT 1
                """
            ).fetchone()
        return row is not None


__all__ = [
    "BrokerAccountSnapshot",
    "BrokerOrderRequest",
    "BrokerOrderRejected",
    "BrokerPort",
    "BrokerSubmission",
    "BrokerWhatIf",
    "ExecutionDesk",
    "LIVE_PORT",
    "SCHEMA_VERSION",
]
