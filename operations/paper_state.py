"""Restart-safe local state and Outbox for the Modern H15 Paper runtime."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import cast

from execution.alpaca_paper import BrokerOrder, PaperPosition

RUN_LEASE = timedelta(seconds=30)


class UnknownBrokerStateError(RuntimeError):
    """Broker state exists that this runtime cannot prove it owns."""


class OutboxClaim(StrEnum):
    CLAIMED = "claimed"
    IN_FLIGHT = "in_flight"
    SENT = "sent"


@dataclass(frozen=True)
class StoredPaperOrder:
    trade_date: date
    client_order_id: str
    broker_order_id: str | None
    symbol: str
    attempt: int
    role: str
    quantity: int
    status: str
    payload: dict[str, object]


class PaperStateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_orders (
                    client_order_id TEXT PRIMARY KEY,
                    trade_date TEXT NOT NULL,
                    broker_order_id TEXT,
                    symbol TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_symbol_state (
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY (trade_date, symbol)
                );
                CREATE TABLE IF NOT EXISTS paper_outbox (
                    event_key TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message_id TEXT,
                    claimed_at_utc TEXT,
                    updated_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_run_lease (
                    trade_date TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    lease_until_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def claim_run(
        self,
        trade_date: date,
        *,
        owner: str,
        observed_at_utc: datetime,
    ) -> bool:
        _require_utc(observed_at_utc)
        if not owner.strip():
            raise ValueError("run lease owner is required")
        day = trade_date.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner, lease_until_utc FROM paper_run_lease WHERE trade_date=?",
                (day,),
            ).fetchone()
            if row is not None:
                lease_until = datetime.fromisoformat(str(row[1]))
                if str(row[0]) != owner and lease_until > observed_at_utc:
                    connection.rollback()
                    return False
            connection.execute(
                """
                INSERT INTO paper_run_lease (
                    trade_date, owner, lease_until_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    owner=excluded.owner,
                    lease_until_utc=excluded.lease_until_utc,
                    updated_at_utc=excluded.updated_at_utc
                """,
                (
                    day,
                    owner,
                    (observed_at_utc + RUN_LEASE).isoformat(),
                    observed_at_utc.isoformat(),
                ),
            )
            connection.commit()
        return True

    def active_run_owner(
        self,
        trade_date: date,
        *,
        observed_at_utc: datetime,
    ) -> str | None:
        """Return the current lease owner without changing lease state."""

        _require_utc(observed_at_utc)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT owner, lease_until_utc FROM paper_run_lease WHERE trade_date=?",
                (trade_date.isoformat(),),
            ).fetchone()
        if row is None:
            return None
        lease_until = datetime.fromisoformat(str(row[1]))
        return str(row[0]) if lease_until > observed_at_utc else None

    def record_order_intent(
        self,
        *,
        trade_date: date,
        client_order_id: str,
        symbol: str,
        attempt: int,
        role: str,
        quantity: int,
        payload: dict[str, object],
        observed_at_utc: datetime,
    ) -> None:
        _require_utc(observed_at_utc)
        if not client_order_id.strip() or not symbol.strip() or not role.strip():
            raise ValueError("order intent identity is required")
        if attempt not in {1, 2} or quantity < 1:
            raise ValueError("order intent attempt and quantity are invalid")
        encoded = _encode(payload)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT trade_date, symbol, attempt, role, quantity, payload_json "
                "FROM paper_orders WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
            identity = (
                trade_date.isoformat(),
                symbol.strip().upper(),
                attempt,
                role.strip(),
                quantity,
                encoded,
            )
            if existing is not None:
                if tuple(existing) != identity:
                    raise RuntimeError("order intent identity changed after persistence")
                return
            connection.execute(
                """
                INSERT INTO paper_orders (
                    client_order_id, trade_date, symbol, attempt, role, quantity,
                    status, payload_json, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, 'intent', ?, ?)
                """,
                (
                    client_order_id,
                    *identity[:-1],
                    encoded,
                    observed_at_utc.isoformat(),
                ),
            )

    def attach_broker_order(
        self,
        *,
        client_order_id: str,
        broker_order_id: str,
        status: str,
        observed_at_utc: datetime,
    ) -> None:
        _require_utc(observed_at_utc)
        if not broker_order_id.strip() or not status.strip():
            raise ValueError("broker order identity is required")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT broker_order_id FROM paper_orders WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("order intent must be persisted before broker submission")
            if row[0] is not None and str(row[0]) != broker_order_id:
                raise RuntimeError("order identity is bound to a different broker order")
            connection.execute(
                """
                UPDATE paper_orders
                SET broker_order_id=?, status=?, updated_at_utc=?
                WHERE client_order_id=?
                """,
                (
                    broker_order_id,
                    status,
                    observed_at_utc.isoformat(),
                    client_order_id,
                ),
            )

    def get_order(self, client_order_id: str) -> StoredPaperOrder | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT trade_date, client_order_id, broker_order_id, symbol,
                       attempt, role, quantity, status, payload_json
                FROM paper_orders WHERE client_order_id=?
                """,
                (client_order_id,),
            ).fetchone()
        if row is None:
            return None
        return StoredPaperOrder(
            trade_date=date.fromisoformat(str(row[0])),
            client_order_id=str(row[1]),
            broker_order_id=None if row[2] is None else str(row[2]),
            symbol=str(row[3]),
            attempt=int(row[4]),
            role=str(row[5]),
            quantity=int(row[6]),
            status=str(row[7]),
            payload=_decode_object(str(row[8])),
        )

    def save_symbol_state(
        self,
        *,
        trade_date: date,
        symbol: str,
        state: dict[str, object],
        observed_at_utc: datetime,
    ) -> None:
        _require_utc(observed_at_utc)
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("symbol state identity is required")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_symbol_state (
                    trade_date, symbol, state_json, updated_at_utc
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(trade_date, symbol) DO UPDATE SET
                    state_json=excluded.state_json,
                    updated_at_utc=excluded.updated_at_utc
                """,
                (
                    trade_date.isoformat(),
                    normalized,
                    _encode(state),
                    observed_at_utc.isoformat(),
                ),
            )

    def delete_symbol_state(self, trade_date: date, symbol: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM paper_symbol_state WHERE trade_date=? AND symbol=?",
                (trade_date.isoformat(), symbol.strip().upper()),
            )

    def load_symbol_states(self, trade_date: date) -> dict[str, dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT symbol, state_json FROM paper_symbol_state WHERE trade_date=?",
                (trade_date.isoformat(),),
            ).fetchall()
        return {str(row[0]): _decode_object(str(row[1])) for row in rows}

    def enqueue_outbox(
        self,
        *,
        event_key: str,
        event_type: str,
        payload: dict[str, object],
        observed_at_utc: datetime,
    ) -> None:
        _require_utc(observed_at_utc)
        if not event_key.strip() or not event_type.strip():
            raise ValueError("outbox identity is required")
        encoded = _encode(payload)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT event_type, payload_json FROM paper_outbox WHERE event_key=?",
                (event_key,),
            ).fetchone()
            if row is not None:
                if tuple(row) != (event_type, encoded):
                    raise RuntimeError("outbox event identity changed")
                return
            connection.execute(
                """
                INSERT INTO paper_outbox (
                    event_key, event_type, payload_json, status, updated_at_utc
                ) VALUES (?, ?, ?, 'pending', ?)
                """,
                (event_key, event_type, encoded, observed_at_utc.isoformat()),
            )

    def claim_outbox(
        self,
        event_key: str,
        *,
        observed_at_utc: datetime,
    ) -> OutboxClaim:
        _require_utc(observed_at_utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM paper_outbox WHERE event_key=?",
                (event_key,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(event_key)
            status = str(row[0])
            if status == "sent":
                connection.rollback()
                return OutboxClaim.SENT
            if status == "sending":
                connection.rollback()
                return OutboxClaim.IN_FLIGHT
            connection.execute(
                """
                UPDATE paper_outbox
                SET status='sending', claimed_at_utc=?, updated_at_utc=?
                WHERE event_key=?
                """,
                (observed_at_utc.isoformat(), observed_at_utc.isoformat(), event_key),
            )
            connection.commit()
        return OutboxClaim.CLAIMED

    def mark_outbox_sent(
        self,
        event_key: str,
        *,
        message_id: str,
        observed_at_utc: datetime,
    ) -> None:
        _require_utc(observed_at_utc)
        if not message_id.strip():
            raise ValueError("outbox message ID is required")
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE paper_outbox
                SET status='sent', message_id=?, updated_at_utc=?
                WHERE event_key=? AND status IN ('sending', 'sent')
                """,
                (message_id, observed_at_utc.isoformat(), event_key),
            ).rowcount
            if updated != 1:
                raise RuntimeError("outbox event must be claimed before delivery")

    def assert_reconcilable(
        self,
        trade_date: date,
        *,
        open_orders: tuple[BrokerOrder, ...],
        positions: tuple[PaperPosition, ...],
    ) -> None:
        states = self.load_symbol_states(trade_date)
        owned_clients = {
            str(value)
            for state in states.values()
            for key, value in state.items()
            if key.endswith("_client_id") and isinstance(value, str)
        }
        with self._connect() as connection:
            owned_clients.update(
                str(row[0])
                for row in connection.execute(
                    "SELECT client_order_id FROM paper_orders WHERE trade_date=?",
                    (trade_date.isoformat(),),
                ).fetchall()
            )
        foreign_orders = [
            order.client_order_id
            for order in open_orders
            if order.client_order_id not in owned_clients
        ]
        foreign_positions = [
            position.symbol
            for position in positions
            if position.symbol not in states or position.side.strip().lower() != "long"
        ]
        if foreign_orders or foreign_positions:
            raise UnknownBrokerStateError(
                "unknown broker state: "
                f"orders={','.join(foreign_orders) or 'none'};"
                f"positions={','.join(foreign_positions) or 'none'}"
            )


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("paper state timestamps must be UTC")


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return {"__datetime_utc__": value.isoformat()}
    raise TypeError(f"unsupported paper state value: {type(value).__name__}")


def _json_hook(value: dict[str, object]) -> object:
    timestamp = value.get("__datetime_utc__")
    if isinstance(timestamp, str) and len(value) == 1:
        return datetime.fromisoformat(timestamp)
    return value


def _encode(value: dict[str, object]) -> str:
    return json.dumps(
        value,
        default=_json_default,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_object(value: str) -> dict[str, object]:
    decoded = json.loads(value, object_hook=_json_hook)
    if not isinstance(decoded, dict):
        raise RuntimeError("persisted Paper state is not an object")
    return cast(dict[str, object], decoded)
