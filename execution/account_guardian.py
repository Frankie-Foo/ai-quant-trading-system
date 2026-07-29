"""Fail-closed Alpaca Paper account exclusivity guardian."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from execution.alpaca_paper import (
    BrokerOrder,
    PaperCloseRequest,
    PaperPosition,
)


class AccountGuardianStatus(StrEnum):
    CLEAR = "clear"
    BLOCKED = "blocked"
    DAY_LOCKED = "day_locked"


@dataclass(frozen=True)
class AccountGuardianResult:
    trade_date: date
    status: AccountGuardianStatus
    new_entries_allowed: bool
    reasons: tuple[str, ...]
    cancelled_order_ids: tuple[str, ...]
    flatten_order_ids: tuple[str, ...]
    provenance: str


class ExclusivePaperBroker(Protocol):
    writes_enabled: bool

    def list_open_orders(self) -> tuple[BrokerOrder, ...]: ...

    def list_positions(self) -> tuple[PaperPosition, ...]: ...

    def cancel_order(self, order_id: str) -> bool: ...

    def submit_close_order_idempotent(
        self, request: PaperCloseRequest
    ) -> BrokerOrder: ...


class AccountGuardianLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS account_guardian_days (
                    trade_date TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    reason TEXT,
                    updated_at_utc TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def status(self, trade_date: date) -> AccountGuardianStatus | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM account_guardian_days WHERE trade_date=?",
                (trade_date.isoformat(),),
            ).fetchone()
        return None if row is None else AccountGuardianStatus(str(row[0]))

    def lock(self, trade_date: date, *, reason: str, at_utc: datetime) -> None:
        _require_utc(at_utc)
        if not reason.strip():
            raise ValueError("account guardian lock reason is required")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO account_guardian_days (
                    trade_date, status, reason, updated_at_utc
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    status=excluded.status,
                    reason=excluded.reason,
                    updated_at_utc=excluded.updated_at_utc
                """,
                (
                    trade_date.isoformat(),
                    AccountGuardianStatus.DAY_LOCKED.value,
                    reason,
                    at_utc.isoformat(),
                ),
            )


class AccountGuardian:
    """Rejects shared/manual account state before any new system entry."""

    def __init__(
        self,
        *,
        broker: ExclusivePaperBroker,
        ledger: AccountGuardianLedger,
        paper_authorized: bool,
        owned_client_order_prefix: str = "tsv2-",
    ):
        if not owned_client_order_prefix:
            raise ValueError("owned client-order prefix is required")
        self.broker = broker
        self.ledger = ledger
        self.paper_authorized = paper_authorized
        self.owned_client_order_prefix = owned_client_order_prefix

    def reconcile(
        self,
        *,
        trade_date: date,
        now_utc: datetime,
        owned_position_symbols: frozenset[str],
    ) -> AccountGuardianResult:
        _require_utc(now_utc)
        if self.ledger.status(trade_date) is AccountGuardianStatus.DAY_LOCKED:
            return AccountGuardianResult(
                trade_date=trade_date,
                status=AccountGuardianStatus.DAY_LOCKED,
                new_entries_allowed=False,
                reasons=("day_lock_already_active",),
                cancelled_order_ids=(),
                flatten_order_ids=(),
                provenance="execution.account_guardian.persisted_day_lock.v1",
            )

        orders = self.broker.list_open_orders()
        positions = self.broker.list_positions()
        unknown_orders = tuple(
            order
            for order in orders
            if not order.client_order_id.startswith(self.owned_client_order_prefix)
        )
        unknown_positions = tuple(
            position
            for position in positions
            if position.symbol not in owned_position_symbols
        )
        reasons = (
            ("unknown_order_detected",)
            if unknown_orders
            else ("unknown_position_detected",)
            if unknown_positions
            else ()
        )
        if not reasons:
            return AccountGuardianResult(
                trade_date=trade_date,
                status=AccountGuardianStatus.CLEAR,
                new_entries_allowed=True,
                reasons=(),
                cancelled_order_ids=(),
                flatten_order_ids=(),
                provenance="execution.account_guardian.exclusive_account_clear.v1",
            )
        if not self.paper_authorized or not self.broker.writes_enabled:
            return AccountGuardianResult(
                trade_date=trade_date,
                status=AccountGuardianStatus.BLOCKED,
                new_entries_allowed=False,
                reasons=reasons,
                cancelled_order_ids=(),
                flatten_order_ids=(),
                provenance="execution.account_guardian.mutation_not_authorized.v1",
            )

        cancelled = tuple(
            order.id for order in orders if self.broker.cancel_order(order.id)
        )
        local_time = now_utc.astimezone(ZoneInfo("America/New_York")).time()
        if not time(9, 30) <= local_time < time(16):
            return AccountGuardianResult(
                trade_date=trade_date,
                status=AccountGuardianStatus.BLOCKED,
                new_entries_allowed=False,
                reasons=reasons
                + ("extended_hours_guardian_flatten_unavailable",),
                cancelled_order_ids=cancelled,
                flatten_order_ids=(),
                provenance="execution.account_guardian.extended_hours_block.v1",
            )
        flattened = tuple(
            self.broker.submit_close_order_idempotent(
                PaperCloseRequest(
                    client_order_id=(
                        f"tsv2-{trade_date:%Y%m%d}-{position.symbol}-guardian-flat"
                    ),
                    symbol=position.symbol,
                    qty=_whole_long_quantity(position),
                )
            ).id
            for position in positions
        )
        self.ledger.lock(trade_date, reason=reasons[0], at_utc=now_utc)
        return AccountGuardianResult(
            trade_date=trade_date,
            status=AccountGuardianStatus.DAY_LOCKED,
            new_entries_allowed=False,
            reasons=reasons,
            cancelled_order_ids=cancelled,
            flatten_order_ids=flattened,
            provenance="execution.account_guardian.cancel_flatten_day_lock.v1",
        )


def _whole_long_quantity(position: PaperPosition) -> int:
    if position.side.strip().lower() != "long":
        raise RuntimeError("account guardian refuses to flatten a non-long position")
    try:
        qty = Decimal(position.qty)
    except InvalidOperation as exc:
        raise RuntimeError("account guardian position quantity is invalid") from exc
    if qty <= 0 or qty != qty.to_integral_value():
        raise RuntimeError("account guardian requires positive whole-share positions")
    return int(qty)


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("account guardian timestamp must be UTC")
