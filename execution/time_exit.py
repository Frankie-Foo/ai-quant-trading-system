"""Idempotent, risk-reducing time exits for bracket-protected long Paper positions."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from db.migrations.sqlite import SQLiteMigration, apply_sqlite_migrations
from execution.alpaca_paper import (
    BrokerOrder,
    PaperCloseRequest,
    PaperPosition,
)
from execution.ledger import OrderLedger
from kernel.tradeplan import TradePlan


class TimeExitStatus(StrEnum):
    PLANNED = "planned"
    SUBMITTED = "submitted"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(frozen=True)
class TimeExitRecord:
    plan_id: str
    client_order_id: str
    symbol: str
    status: TimeExitStatus
    quantity: int | None
    broker_order_id: str | None
    updated_at_utc: datetime
    provenance: str


@dataclass(frozen=True)
class TimeExitResult:
    plan_id: str
    symbol: str
    status: TimeExitStatus
    dry_run: bool
    broker_order_id: str | None
    cancelled_order_ids: tuple[str, ...]
    detail: str


class TimeExitBroker(Protocol):
    writes_enabled: bool

    def list_positions(self) -> tuple[PaperPosition, ...]: ...

    def list_open_orders(self) -> tuple[BrokerOrder, ...]: ...

    def cancel_order(self, order_id: str) -> bool: ...

    def submit_close_order_idempotent(
        self, request: PaperCloseRequest
    ) -> BrokerOrder: ...


def _create_time_exit_actions(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS time_exit_actions (
            plan_id TEXT PRIMARY KEY,
            client_order_id TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            status TEXT NOT NULL,
            quantity INTEGER,
            broker_order_id TEXT,
            updated_at_utc TEXT NOT NULL,
            provenance TEXT NOT NULL
        )
        """
    )


TIME_EXIT_LEDGER_MIGRATIONS = (
    SQLiteMigration(
        version=1,
        name="time_exit_actions",
        signature="time_exit_actions.v1",
        apply=_create_time_exit_actions,
    ),
)


class TimeExitLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            apply_sqlite_migrations(
                connection,
                owner="execution.time_exit_ledger",
                migrations=TIME_EXIT_LEDGER_MIGRATIONS,
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @staticmethod
    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("time-exit timestamp must be timezone-aware UTC")

    @staticmethod
    def _read(row: sqlite3.Row | None) -> TimeExitRecord | None:
        if row is None:
            return None
        return TimeExitRecord(
            plan_id=str(row["plan_id"]),
            client_order_id=str(row["client_order_id"]),
            symbol=str(row["symbol"]),
            status=TimeExitStatus(str(row["status"])),
            quantity=int(row["quantity"]) if row["quantity"] is not None else None,
            broker_order_id=(
                str(row["broker_order_id"])
                if row["broker_order_id"] is not None
                else None
            ),
            updated_at_utc=datetime.fromisoformat(str(row["updated_at_utc"])),
            provenance=str(row["provenance"]),
        )

    def get(self, plan_id: str) -> TimeExitRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM time_exit_actions WHERE plan_id=?", (plan_id,)
            ).fetchone()
        return self._read(row)

    def ensure(
        self,
        plan: TradePlan,
        *,
        client_order_id: str,
        at_utc: datetime,
    ) -> TimeExitRecord:
        self._require_utc(at_utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM time_exit_actions WHERE plan_id=?", (plan.plan_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO time_exit_actions (
                        plan_id, client_order_id, symbol, status, quantity,
                        broker_order_id, updated_at_utc, provenance
                    ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        plan.plan_id,
                        client_order_id,
                        plan.symbol,
                        TimeExitStatus.PLANNED.value,
                        at_utc.isoformat(),
                        "execution.time_exit.planned.v1",
                    ),
                )
                existing = connection.execute(
                    "SELECT * FROM time_exit_actions WHERE plan_id=?", (plan.plan_id,)
                ).fetchone()
            record = self._read(existing)
            if record is None:
                raise RuntimeError("time-exit record disappeared")
            if record.client_order_id != client_order_id or record.symbol != plan.symbol:
                raise RuntimeError("time-exit identity conflict")
            return record

    def update(
        self,
        plan_id: str,
        *,
        status: TimeExitStatus,
        at_utc: datetime,
        provenance: str,
        quantity: int | None = None,
        broker_order_id: str | None = None,
    ) -> TimeExitRecord:
        self._require_utc(at_utc)
        if not provenance.strip():
            raise ValueError("time-exit provenance is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._read(
                connection.execute(
                    "SELECT * FROM time_exit_actions WHERE plan_id=?", (plan_id,)
                ).fetchone()
            )
            if current is None:
                raise KeyError(f"unknown time-exit plan: {plan_id}")
            if current.status is TimeExitStatus.COMPLETE:
                return current
            connection.execute(
                """
                UPDATE time_exit_actions
                SET status=?, quantity=COALESCE(?, quantity),
                    broker_order_id=COALESCE(?, broker_order_id),
                    updated_at_utc=?, provenance=?
                WHERE plan_id=?
                """,
                (
                    status.value,
                    quantity,
                    broker_order_id,
                    at_utc.isoformat(),
                    provenance,
                    plan_id,
                ),
            )
        updated = self.get(plan_id)
        if updated is None:
            raise RuntimeError("time-exit update disappeared")
        return updated


def _time_exit_client_id(plan: TradePlan) -> str:
    digest = hashlib.sha256(plan.plan_id.encode()).hexdigest()[:16]
    return f"tsv2-{plan.trade_date:%Y%m%d}-{plan.symbol}-time-{digest}"


def _long_quantity(position: PaperPosition) -> int:
    if position.side.strip().lower() != "long":
        raise ValueError("time exit refuses any non-long position")
    try:
        value = Decimal(position.qty)
    except InvalidOperation as exc:
        raise ValueError("position quantity is invalid") from exc
    if value <= 0 or value != value.to_integral_value():
        raise ValueError("time exit requires a positive whole-share position")
    return int(value)


class TimeExitCoordinator:
    def __init__(
        self,
        *,
        order_ledger: OrderLedger,
        exit_ledger: TimeExitLedger,
        broker: TimeExitBroker,
        paper_authorized: bool,
    ):
        self.order_ledger = order_ledger
        self.exit_ledger = exit_ledger
        self.broker = broker
        self.paper_authorized = paper_authorized

    def run_due(self, *, now_utc: datetime) -> tuple[TimeExitResult, ...]:
        if now_utc.tzinfo is None or now_utc.utcoffset() != timedelta(0):
            raise ValueError("now_utc must be timezone-aware UTC")
        due = [
            plan
            for plan in self.order_ledger.list_plans()
            if plan.time_stop_utc <= now_utc
        ]
        return tuple(self._run_plan(plan, now_utc=now_utc) for plan in due)

    def _run_plan(self, plan: TradePlan, *, now_utc: datetime) -> TimeExitResult:
        client_order_id = _time_exit_client_id(plan)
        record = self.exit_ledger.ensure(
            plan, client_order_id=client_order_id, at_utc=now_utc
        )
        if record.status is TimeExitStatus.COMPLETE:
            return TimeExitResult(
                plan.plan_id,
                plan.symbol,
                record.status,
                False,
                record.broker_order_id,
                (),
                record.provenance,
            )
        if not self.paper_authorized or not self.broker.writes_enabled:
            return TimeExitResult(
                plan.plan_id,
                plan.symbol,
                record.status,
                True,
                record.broker_order_id,
                (),
                "paper time-exit writes are not authorized",
            )

        cancelled: list[str] = []
        for order in self.broker.list_open_orders():
            if order.symbol != plan.symbol or order.client_order_id == client_order_id:
                continue
            if self.broker.cancel_order(order.id):
                cancelled.append(order.id)

        positions = {
            position.symbol: position for position in self.broker.list_positions()
        }
        position = positions.get(plan.symbol)
        if position is None:
            completed = self.exit_ledger.update(
                plan.plan_id,
                status=TimeExitStatus.COMPLETE,
                at_utc=now_utc,
                provenance="execution.time_exit.no_position_after_order_cancel.v1",
            )
            return TimeExitResult(
                plan.plan_id,
                plan.symbol,
                completed.status,
                False,
                completed.broker_order_id,
                tuple(cancelled),
                completed.provenance,
            )

        quantity = _long_quantity(position)
        close_order = self.broker.submit_close_order_idempotent(
            PaperCloseRequest(
                client_order_id=client_order_id,
                symbol=plan.symbol,
                qty=quantity,
            )
        )
        status = (
            TimeExitStatus.COMPLETE
            if close_order.status.strip().lower() == "filled"
            else TimeExitStatus.FAILED
            if close_order.status.strip().lower() in {"rejected", "canceled", "expired"}
            else TimeExitStatus.SUBMITTED
        )
        updated = self.exit_ledger.update(
            plan.plan_id,
            status=status,
            at_utc=now_utc,
            provenance=f"alpaca.paper.time_exit:{close_order.id}:{close_order.status}",
            quantity=quantity,
            broker_order_id=close_order.id,
        )
        return TimeExitResult(
            plan.plan_id,
            plan.symbol,
            updated.status,
            False,
            updated.broker_order_id,
            tuple(cancelled),
            updated.provenance,
        )
