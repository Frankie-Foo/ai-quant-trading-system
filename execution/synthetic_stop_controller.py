"""Durable, idempotent executor for extended-hours synthetic stops."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from db.migrations.sqlite import SQLiteMigration, apply_sqlite_migrations
from execution.alpaca_paper import (
    BrokerOrder,
    PaperExtendedLimitRequest,
    PaperPosition,
)
from execution.synthetic_stop import (
    StopAction,
    SyntheticStopDecision,
    SyntheticStopEngine,
    SyntheticStopPlan,
    SyntheticStopRuntime,
    SyntheticStopSnapshot,
)


class SyntheticStopBroker(Protocol):
    writes_enabled: bool

    def list_positions(self) -> tuple[PaperPosition, ...]: ...

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...

    def cancel_order(self, order_id: str) -> bool: ...

    def submit_extended_limit_idempotent(
        self, request: PaperExtendedLimitRequest
    ) -> BrokerOrder: ...


@dataclass(frozen=True)
class SyntheticStopExecutionRecord:
    plan: SyntheticStopPlan
    runtime: SyntheticStopRuntime
    active_client_order_id: str | None
    active_broker_order_id: str | None
    active_limit_price: Decimal | None
    active_qty: int | None
    updated_at_utc: datetime


@dataclass(frozen=True)
class SyntheticStopExecutionResult:
    action: StopAction
    runtime: SyntheticStopRuntime
    limit_price: Decimal | None
    reasons: tuple[str, ...]
    blockers: tuple[str, ...]
    dry_run: bool
    broker_order_id: str | None


def _create_synthetic_stop_actions(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS synthetic_stop_actions (
            plan_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            qty INTEGER NOT NULL,
            stop_price TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            runtime_json TEXT NOT NULL,
            active_client_order_id TEXT,
            active_broker_order_id TEXT,
            active_limit_price TEXT,
            active_qty INTEGER,
            updated_at_utc TEXT NOT NULL
        )
        """
    )


SYNTHETIC_STOP_LEDGER_MIGRATIONS = (
    SQLiteMigration(
        version=1,
        name="synthetic_stop_actions",
        signature="synthetic_stop_actions.v1",
        apply=_create_synthetic_stop_actions,
    ),
)


class SyntheticStopExecutionLedger:
    """SQLite outbox: command intent is durable before a broker write occurs."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            apply_sqlite_migrations(
                connection,
                owner="execution.synthetic_stop_ledger",
                migrations=SYNTHETIC_STOP_LEDGER_MIGRATIONS,
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def ensure(
        self, plan: SyntheticStopPlan, *, at_utc: datetime
    ) -> SyntheticStopExecutionRecord:
        _require_utc(at_utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM synthetic_stop_actions WHERE plan_id=?",
                (plan.plan_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO synthetic_stop_actions (
                        plan_id, symbol, qty, stop_price, plan_json, runtime_json,
                        active_client_order_id, active_broker_order_id,
                        active_limit_price, active_qty, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)
                    """,
                    (
                        plan.plan_id,
                        plan.symbol,
                        plan.qty,
                        str(plan.stop_price),
                        _plan_json(plan),
                        _runtime_json(SyntheticStopRuntime.initial()),
                        at_utc.isoformat(),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM synthetic_stop_actions WHERE plan_id=?",
                    (plan.plan_id,),
                ).fetchone()
        record = _record_from_row(row)
        if record.plan != plan:
            raise RuntimeError("synthetic-stop plan identity conflict")
        return record

    def save(
        self,
        plan_id: str,
        *,
        runtime: SyntheticStopRuntime,
        at_utc: datetime,
        active_client_order_id: str | None,
        active_broker_order_id: str | None,
        active_limit_price: Decimal | None,
        active_qty: int | None,
    ) -> SyntheticStopExecutionRecord:
        _require_utc(at_utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE synthetic_stop_actions
                SET runtime_json=?, active_client_order_id=?,
                    active_broker_order_id=?, active_limit_price=?,
                    active_qty=?, updated_at_utc=?
                WHERE plan_id=?
                """,
                (
                    _runtime_json(runtime),
                    active_client_order_id,
                    active_broker_order_id,
                    (
                        str(active_limit_price)
                        if active_limit_price is not None
                        else None
                    ),
                    active_qty,
                    at_utc.isoformat(),
                    plan_id,
                ),
            ).rowcount
            if changed != 1:
                raise KeyError(f"unknown synthetic-stop plan: {plan_id}")
            row = connection.execute(
                "SELECT * FROM synthetic_stop_actions WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
        return _record_from_row(row)


class SyntheticStopController:
    def __init__(
        self,
        *,
        broker: SyntheticStopBroker,
        ledger: SyntheticStopExecutionLedger,
        paper_authorized: bool,
        engine: SyntheticStopEngine | None = None,
    ):
        self.broker = broker
        self.ledger = ledger
        self.paper_authorized = paper_authorized
        self.engine = engine or SyntheticStopEngine()

    def tick(
        self,
        plan: SyntheticStopPlan,
        snapshot: SyntheticStopSnapshot,
    ) -> SyntheticStopExecutionResult:
        record = self.ledger.ensure(plan, at_utc=snapshot.observed_at_utc)
        authorized = self.paper_authorized and self.broker.writes_enabled

        if authorized:
            record = self._recover_pending_outbox(record, snapshot=snapshot)

        position_qty = _position_quantity(
            plan.symbol,
            self.broker.list_positions(),
        )
        effective_snapshot = (
            replace(snapshot, filled=True) if position_qty == 0 else snapshot
        )
        decision = self.engine.evaluate(
            plan,
            record.runtime,
            effective_snapshot,
        )
        if decision.action in {
            StopAction.SUBMIT_EXIT_LIMIT,
            StopAction.CANCEL_REPLACE_EXIT,
        } and not authorized:
            return SyntheticStopExecutionResult(
                action=StopAction.ALERT,
                runtime=record.runtime,
                limit_price=decision.limit_price,
                reasons=decision.reasons,
                blockers=decision.blockers + ("paper_writes_not_authorized",),
                dry_run=True,
                broker_order_id=record.active_broker_order_id,
            )

        if decision.action is StopAction.SUBMIT_EXIT_LIMIT:
            if decision.limit_price is None:
                raise RuntimeError("synthetic stop submit lacks limit price")
            record = self._submit(
                record,
                decision,
                qty=position_qty,
                snapshot=snapshot,
            )
        elif decision.action is StopAction.CANCEL_REPLACE_EXIT:
            if decision.limit_price is None:
                raise RuntimeError("synthetic stop reprice lacks limit price")
            if record.active_broker_order_id is not None:
                self.broker.cancel_order(record.active_broker_order_id)
            record = self._submit(
                record,
                decision,
                qty=position_qty,
                snapshot=snapshot,
            )
        else:
            record = self.ledger.save(
                plan.plan_id,
                runtime=decision.runtime,
                at_utc=snapshot.observed_at_utc,
                active_client_order_id=record.active_client_order_id,
                active_broker_order_id=record.active_broker_order_id,
                active_limit_price=record.active_limit_price,
                active_qty=record.active_qty,
            )
        return _result(
            decision,
            dry_run=False,
            broker_order_id=record.active_broker_order_id,
        )

    def _recover_pending_outbox(
        self,
        record: SyntheticStopExecutionRecord,
        *,
        snapshot: SyntheticStopSnapshot,
    ) -> SyntheticStopExecutionRecord:
        client_id = record.active_client_order_id
        if client_id is None:
            return record
        order = self.broker.get_order_by_client_id(client_id)
        if order is None and record.active_broker_order_id is None:
            if record.active_limit_price is None or record.active_qty is None:
                raise RuntimeError("synthetic-stop outbox command is incomplete")
            order = self.broker.submit_extended_limit_idempotent(
                _request(
                    record.plan,
                    client_id=client_id,
                    qty=record.active_qty,
                    limit_price=record.active_limit_price,
                )
            )
        if order is None or order.id == record.active_broker_order_id:
            return record
        return self.ledger.save(
            record.plan.plan_id,
            runtime=record.runtime,
            at_utc=snapshot.observed_at_utc,
            active_client_order_id=client_id,
            active_broker_order_id=order.id,
            active_limit_price=record.active_limit_price,
            active_qty=record.active_qty,
        )

    def _submit(
        self,
        record: SyntheticStopExecutionRecord,
        decision: SyntheticStopDecision,
        *,
        qty: int,
        snapshot: SyntheticStopSnapshot,
    ) -> SyntheticStopExecutionRecord:
        if qty <= 0 or decision.limit_price is None:
            raise RuntimeError("synthetic stop refuses a zero-quantity sell")
        client_id = _client_order_id(
            record.plan,
            attempt=decision.runtime.price_attempt,
        )
        pending = self.ledger.save(
            record.plan.plan_id,
            runtime=decision.runtime,
            at_utc=snapshot.observed_at_utc,
            active_client_order_id=client_id,
            active_broker_order_id=None,
            active_limit_price=decision.limit_price,
            active_qty=qty,
        )
        order = self.broker.get_order_by_client_id(client_id)
        if order is None:
            order = self.broker.submit_extended_limit_idempotent(
                _request(
                    record.plan,
                    client_id=client_id,
                    qty=qty,
                    limit_price=decision.limit_price,
                )
            )
        return self.ledger.save(
            record.plan.plan_id,
            runtime=decision.runtime,
            at_utc=snapshot.observed_at_utc,
            active_client_order_id=client_id,
            active_broker_order_id=order.id,
            active_limit_price=pending.active_limit_price,
            active_qty=pending.active_qty,
        )


def _request(
    plan: SyntheticStopPlan,
    *,
    client_id: str,
    qty: int,
    limit_price: Decimal,
) -> PaperExtendedLimitRequest:
    return PaperExtendedLimitRequest(
        client_order_id=client_id,
        symbol=plan.symbol,
        qty=qty,
        side="sell",
        limit_price=f"{limit_price:.2f}",
    )


def _client_order_id(plan: SyntheticStopPlan, *, attempt: int) -> str:
    digest = hashlib.sha256(plan.plan_id.encode()).hexdigest()[:16]
    return f"tsv2-{plan.symbol}-sstop-{digest}-{attempt}"


def _position_quantity(
    symbol: str,
    positions: tuple[PaperPosition, ...],
) -> int:
    matching = [position for position in positions if position.symbol == symbol]
    if not matching:
        return 0
    if len(matching) != 1 or matching[0].side.strip().lower() != "long":
        raise RuntimeError("synthetic stop requires one reconciled long position")
    try:
        value = Decimal(matching[0].qty)
    except InvalidOperation as exc:
        raise RuntimeError("synthetic stop position quantity is invalid") from exc
    if value <= 0 or value != value.to_integral_value():
        raise RuntimeError("synthetic stop requires positive whole-share quantity")
    return int(value)


def _result(
    decision: SyntheticStopDecision,
    *,
    dry_run: bool,
    broker_order_id: str | None,
) -> SyntheticStopExecutionResult:
    return SyntheticStopExecutionResult(
        action=decision.action,
        runtime=decision.runtime,
        limit_price=decision.limit_price,
        reasons=decision.reasons,
        blockers=decision.blockers,
        dry_run=dry_run,
        broker_order_id=broker_order_id,
    )


def _plan_json(plan: SyntheticStopPlan) -> str:
    return json.dumps(
        {
            "plan_id": plan.plan_id,
            "symbol": plan.symbol,
            "qty": plan.qty,
            "stop_price": str(plan.stop_price),
            "confirmation_seconds": plan.confirmation_seconds,
            "reprice_seconds": plan.reprice_seconds,
            "price_buffers": [str(value) for value in plan.price_buffers],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _runtime_json(runtime: SyntheticStopRuntime) -> str:
    return json.dumps(
        {
            "below_since_utc": _iso(runtime.below_since_utc),
            "triggered_at_utc": _iso(runtime.triggered_at_utc),
            "last_command_at_utc": _iso(runtime.last_command_at_utc),
            "price_attempt": runtime.price_attempt,
            "completed_at_utc": _iso(runtime.completed_at_utc),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _record_from_row(row: sqlite3.Row | None) -> SyntheticStopExecutionRecord:
    if row is None:
        raise RuntimeError("synthetic-stop execution record disappeared")
    plan_payload = json.loads(str(row["plan_json"]))
    runtime_payload = json.loads(str(row["runtime_json"]))
    return SyntheticStopExecutionRecord(
        plan=SyntheticStopPlan(
            plan_id=str(plan_payload["plan_id"]),
            symbol=str(plan_payload["symbol"]),
            qty=int(plan_payload["qty"]),
            stop_price=Decimal(str(plan_payload["stop_price"])),
            confirmation_seconds=float(plan_payload["confirmation_seconds"]),
            reprice_seconds=float(plan_payload["reprice_seconds"]),
            price_buffers=tuple(
                Decimal(str(value)) for value in plan_payload["price_buffers"]
            ),
        ),
        runtime=SyntheticStopRuntime(
            below_since_utc=_datetime(runtime_payload["below_since_utc"]),
            triggered_at_utc=_datetime(runtime_payload["triggered_at_utc"]),
            last_command_at_utc=_datetime(runtime_payload["last_command_at_utc"]),
            price_attempt=int(runtime_payload["price_attempt"]),
            completed_at_utc=_datetime(runtime_payload["completed_at_utc"]),
        ),
        active_client_order_id=(
            str(row["active_client_order_id"])
            if row["active_client_order_id"] is not None
            else None
        ),
        active_broker_order_id=(
            str(row["active_broker_order_id"])
            if row["active_broker_order_id"] is not None
            else None
        ),
        active_limit_price=(
            Decimal(str(row["active_limit_price"]))
            if row["active_limit_price"] is not None
            else None
        ),
        active_qty=(
            int(row["active_qty"]) if row["active_qty"] is not None else None
        ),
        updated_at_utc=datetime.fromisoformat(str(row["updated_at_utc"])),
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _datetime(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("synthetic-stop execution timestamp must be UTC")
