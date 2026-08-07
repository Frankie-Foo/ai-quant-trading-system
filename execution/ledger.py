"""Durable, idempotent SQLite order ledger."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from db.migrations.sqlite import SQLiteMigration, apply_sqlite_migrations
from execution.order_state import OrderLifecycle, OrderState, apply_transition
from kernel.tradeplan import TradePlan


class OrderLedgerConflictError(RuntimeError):
    """A client order ID was reused for a different intent."""


def _create_order_ledger_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            client_order_id TEXT PRIMARY KEY,
            broker_order_id TEXT UNIQUE,
            plan_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            requested_shares INTEGER NOT NULL CHECK(requested_shares > 0),
            state TEXT NOT NULL,
            lifecycle_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS order_events (
            client_order_id TEXT NOT NULL REFERENCES orders(client_order_id),
            sequence INTEGER NOT NULL CHECK(sequence > 0),
            at_utc TEXT NOT NULL,
            from_state TEXT NOT NULL,
            to_state TEXT NOT NULL,
            filled_shares INTEGER NOT NULL CHECK(filled_shares >= 0),
            provenance TEXT NOT NULL,
            PRIMARY KEY (client_order_id, sequence)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_plans (
            plan_id TEXT PRIMARY KEY,
            client_order_id TEXT NOT NULL UNIQUE,
            trace_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        )
        """
    )


ORDER_LEDGER_MIGRATIONS = (
    SQLiteMigration(
        version=1,
        name="orders_and_trade_plans",
        signature="execution.order_ledger.v1",
        apply=_create_order_ledger_schema,
    ),
)


class OrderLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            apply_sqlite_migrations(
                connection,
                owner="execution.order_ledger",
                migrations=ORDER_LEDGER_MIGRATIONS,
            )

    def get_plan(self, plan_id: str) -> TradePlan | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT plan_json FROM trade_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        return None if row is None else TradePlan.model_validate_json(str(row[0]))

    def list_plans(self) -> tuple[TradePlan, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT plan_json FROM trade_plans ORDER BY created_at_utc, plan_id"
            ).fetchall()
        return tuple(TradePlan.model_validate_json(str(row[0])) for row in rows)

    def record_plan(self, plan: TradePlan) -> TradePlan:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT plan_json FROM trade_plans WHERE plan_id = ?",
                (plan.plan_id,),
            ).fetchone()
            if row is not None:
                existing = TradePlan.model_validate_json(str(row[0]))
                if existing != plan:
                    raise OrderLedgerConflictError(
                        "plan_id already belongs to a different TradePlan"
                    )
                connection.commit()
                return existing
            connection.execute(
                """
                INSERT INTO trade_plans (
                    plan_id, client_order_id, trace_id, symbol, trade_date,
                    plan_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.client_order_id,
                    plan.trace_id,
                    plan.symbol,
                    plan.trade_date.isoformat(),
                    plan.model_dump_json(),
                    plan.created_at_utc.isoformat(),
                ),
            )
            connection.commit()
            return plan

    def get_broker_order_id(self, client_order_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT broker_order_id FROM orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown client_order_id: {client_order_id}")
        return None if row[0] is None else str(row[0])

    def record_broker_order_id(self, client_order_id: str, broker_order_id: str) -> None:
        if not broker_order_id.strip():
            raise ValueError("broker_order_id is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT broker_order_id FROM orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown client_order_id: {client_order_id}")
            existing = None if row[0] is None else str(row[0])
            if existing is not None and existing != broker_order_id:
                raise OrderLedgerConflictError(
                    "client order ID is already bound to a different broker order"
                )
            connection.execute(
                "UPDATE orders SET broker_order_id = ? WHERE client_order_id = ?",
                (broker_order_id, client_order_id),
            )
            connection.commit()

    @staticmethod
    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("ledger timestamps must be timezone-aware UTC")

    @staticmethod
    def _read(connection: sqlite3.Connection, client_order_id: str) -> OrderLifecycle | None:
        row = connection.execute(
            "SELECT lifecycle_json FROM orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        return None if row is None else OrderLifecycle.model_validate_json(str(row[0]))

    def get(self, client_order_id: str) -> OrderLifecycle | None:
        with self._connect() as connection:
            return self._read(connection, client_order_id)

    def list_orders(self) -> tuple[OrderLifecycle, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT lifecycle_json FROM orders ORDER BY created_at_utc, client_order_id"
            ).fetchall()
        return tuple(OrderLifecycle.model_validate_json(str(row[0])) for row in rows)

    def create(self, order: OrderLifecycle, *, created_at_utc: datetime) -> OrderLifecycle:
        self._require_utc(created_at_utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._read(connection, order.client_order_id)
            if existing is not None:
                same_intent = (
                    existing.plan_id == order.plan_id
                    and existing.symbol == order.symbol
                    and existing.requested_shares == order.requested_shares
                )
                if not same_intent:
                    raise OrderLedgerConflictError(
                        "client_order_id already belongs to a different order intent"
                    )
                connection.commit()
                return existing
            timestamp = created_at_utc.isoformat()
            connection.execute(
                """
                INSERT INTO orders (
                    client_order_id, plan_id, symbol, requested_shares, state,
                    lifecycle_json, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.client_order_id,
                    order.plan_id,
                    order.symbol,
                    order.requested_shares,
                    order.state.value,
                    order.model_dump_json(),
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
            return order

    def transition(
        self,
        client_order_id: str,
        next_state: OrderState,
        *,
        at_utc: datetime,
        provenance: str,
        filled_shares: int | None = None,
    ) -> OrderLifecycle:
        self._require_utc(at_utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._read(connection, client_order_id)
            if current is None:
                raise KeyError(f"unknown client_order_id: {client_order_id}")
            updated = apply_transition(
                current,
                next_state,
                at_utc=at_utc,
                provenance=provenance,
                filled_shares=filled_shares,
            )
            event = updated.events[-1]
            connection.execute(
                """
                UPDATE orders
                SET state = ?, lifecycle_json = ?, updated_at_utc = ?
                WHERE client_order_id = ?
                """,
                (
                    updated.state.value,
                    updated.model_dump_json(),
                    at_utc.isoformat(),
                    client_order_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO order_events (
                    client_order_id, sequence, at_utc, from_state, to_state,
                    filled_shares, provenance
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_order_id,
                    event.sequence,
                    event.at_utc.isoformat(),
                    event.from_state.value,
                    event.to_state.value,
                    event.filled_shares,
                    event.provenance,
                ),
            )
            connection.commit()
            return updated
