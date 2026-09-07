"""Restart-safe local state and Outbox for the Modern H15 Paper runtime."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import cast

from execution.alpaca_paper import BrokerOrder, PaperPosition

RUN_LEASE = timedelta(seconds=30)
TERMINAL_ORDER_STATUSES = frozenset({"filled", "canceled", "expired", "rejected"})


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


@dataclass(frozen=True)
class PriorDayPaperState:
    trade_date: date
    path: Path
    states: dict[str, dict[str, object]]
    orders: tuple[StoredPaperOrder, ...]


def discover_prior_day_stores(root: Path, *, trade_date: date) -> tuple[Path, ...]:
    """Read historical daily stores without initialization, migration or authorization."""
    resolved_root = root.resolve()
    found: list[Path] = []
    for path in sorted(root.glob("*/paper-state.sqlite3")):
        try:
            source_date = date.fromisoformat(path.parent.name)
        except ValueError:
            continue
        if source_date >= trade_date:
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(resolved_root):
            raise RuntimeError("historical Paper store escaped the supplied root")
        with closing(sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("BEGIN")
            states = connection.execute("SELECT state_json FROM paper_symbol_state").fetchall()
            orders = connection.execute(
                "SELECT status, role, broker_order_id FROM paper_orders"
            ).fetchall()
            if any(_decode_object(str(row[0])).get("phase") != "complete" for row in states) or any(
                str(row[0]).lower() not in TERMINAL_ORDER_STATUSES for row in orders
                if tuple(row) != ("aborted", "entry", None)
            ):
                found.append(resolved)
    return tuple(found)


def read_prior_day_states(root: Path, *, trade_date: date) -> dict[date, PriorDayPaperState]:
    """Return historical evidence, not broker ownership or entry authorization."""
    history: dict[date, PriorDayPaperState] = {}
    for path in discover_prior_day_stores(root, trade_date=trade_date):
        source_date = date.fromisoformat(path.parent.name)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("BEGIN")
            states = connection.execute(
                "SELECT symbol, state_json FROM paper_symbol_state WHERE trade_date=?",
                (source_date.isoformat(),),
            ).fetchall()
            orders = connection.execute(
                "SELECT trade_date, client_order_id, broker_order_id, symbol, "
                "attempt, role, quantity, status, payload_json FROM paper_orders "
                "ORDER BY client_order_id"
            ).fetchall()
        history[source_date] = PriorDayPaperState(
            trade_date=source_date, path=path,
            states={str(row[0]): _decode_object(str(row[1])) for row in states},
            orders=tuple(_stored_order(row) for row in orders),
        )
    return history


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

    def abort_unsubmitted_entry(
        self, *, client_order_id: str, prior_state: dict[str, object] | None,
        observed_at_utc: datetime,
    ) -> None:
        """Restore the symbol only after the caller proves POST was never attempted."""
        _require_utc(observed_at_utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT trade_date, symbol, role, status, broker_order_id FROM paper_orders "
                "WHERE client_order_id=?", (client_order_id,),
            ).fetchone()
            if row is None or tuple(row[2:]) != ("entry", "intent", None):
                raise RuntimeError("only an unbound entry intent may be aborted")
            current = connection.execute(
                "SELECT state_json FROM paper_symbol_state WHERE trade_date=? AND symbol=?",
                row[:2],
            ).fetchone()
            state = _decode_object(str(current[0])) if current else {}
            if (
                state.get("phase") != "entry_pending"
                or state.get("entry_client_id") != client_order_id
            ):
                raise RuntimeError("pending entry state no longer matches aborted intent")
            connection.execute(
                "UPDATE paper_orders SET status='aborted', updated_at_utc=? "
                "WHERE client_order_id=?",
                (observed_at_utc.isoformat(), client_order_id),
            )
            if prior_state is None:
                connection.execute(
                    "DELETE FROM paper_symbol_state WHERE trade_date=? AND symbol=?", row[:2],
                )
            else:
                connection.execute(
                    "UPDATE paper_symbol_state SET state_json=?, updated_at_utc=? "
                    "WHERE trade_date=? AND symbol=?",
                    (_encode(prior_state), observed_at_utc.isoformat(), *row[:2]),
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
        return _stored_order(row)

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

    def list_orders(self) -> tuple[StoredPaperOrder, ...]:
        """Read every persisted attempt, including imported and recovery-day exits."""
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            rows = conn.execute(
                "SELECT trade_date, client_order_id, broker_order_id, symbol, "
                "attempt, role, quantity, status, payload_json FROM paper_orders "
                "ORDER BY client_order_id"
            ).fetchall()
        return tuple(_stored_order(row) for row in rows)

    def import_exit_recovery(
        self,
        source_path: Path,
        *,
        source_trade_date: date,
        trade_date: date,
        observed_at_utc: datetime,
    ) -> dict[str, dict[str, object]]:
        """Copy historical facts into an isolated recovery store, never authorize orders.

        The caller must verify broker entry IDs, fills, residual quantities and child
        orders before explicit exit-only execution. Saved entry requests remain audit
        data, not instructions to submit. The historical database is opened read-only.
        """
        _require_utc(observed_at_utc)
        source = source_path.resolve()
        if source_trade_date >= trade_date or source == self.path.resolve():
            raise ValueError("exit recovery requires a distinct prior-day source")
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as previous:
            previous.execute("BEGIN")
            leases = previous.execute("SELECT lease_until_utc FROM paper_run_lease").fetchall()
            if any(datetime.fromisoformat(str(row[0])) > observed_at_utc for row in leases):
                raise RuntimeError("historical Paper monitor lease is active")
            states = previous.execute(
                "SELECT symbol, state_json FROM paper_symbol_state "
                "WHERE trade_date=? ORDER BY symbol",
                (source_trade_date.isoformat(),),
            ).fetchall()
            orders = previous.execute(
                "SELECT client_order_id, trade_date, broker_order_id, symbol, attempt, role, "
                "quantity, status, payload_json, updated_at_utc FROM paper_orders "
                "ORDER BY client_order_id"
            ).fetchall()
            outbox = previous.execute(
                "SELECT event_key, event_type, payload_json, status, message_id, "
                "claimed_at_utc, updated_at_utc FROM paper_outbox ORDER BY event_key"
            ).fetchall()
        if any(date.fromisoformat(str(row[1])) > source_trade_date for row in orders):
            raise RuntimeError("historical order date exceeds the recovery source date")
        source_hash = hashlib.sha256(
            json.dumps([states, orders, outbox], sort_keys=True).encode("utf-8")
        ).hexdigest()
        imported: dict[str, dict[str, object]] = {}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for symbol, encoded in states:
                state = _decode_object(str(encoded))
                if state.get("phase") == "complete":
                    continue
                if state.get("phase") not in {"entry_pending", "active", "exit_pending", "stopped"}:
                    raise RuntimeError("historical Paper state has an unknown phase")
                existing = connection.execute(
                    "SELECT state_json FROM paper_symbol_state WHERE trade_date=? AND symbol=?",
                    (trade_date.isoformat(), symbol),
                ).fetchone()
                if existing is not None:
                    current = _decode_object(str(existing[0]))
                    if (
                        current.get("recovery_source_hash") != source_hash
                        or current.get("recovery_source_path") != str(source)
                        or current.get("recovery_only") is not True
                    ):
                        raise RuntimeError("exit recovery conflicts with existing symbol state")
                    imported[str(symbol)] = current
                    continue
                state.update(
                    recovery_only=True,
                    recovery_source_trade_date=source_trade_date.isoformat(),
                    recovery_source_path=str(source),
                    recovery_source_hash=source_hash,
                )
                connection.execute(
                    "INSERT INTO paper_symbol_state VALUES (?, ?, ?, ?)",
                    (trade_date.isoformat(), symbol, _encode(state), observed_at_utc.isoformat()),
                )
                imported[str(symbol)] = state
            for row in orders:
                if (
                    str(row[7]).lower() not in TERMINAL_ORDER_STATUSES
                    and (row[7], row[5], row[2]) != ("aborted", "entry", None)
                    and str(row[3]) not in imported
                ):
                    raise RuntimeError("historical live order has no recoverable symbol state")
                existing = connection.execute(
                    "SELECT trade_date, symbol, attempt, role, quantity, "
                    "payload_json, broker_order_id "
                    "FROM paper_orders WHERE client_order_id=?", (row[0],),
                ).fetchone()
                if existing is not None:
                    if tuple(existing[:6]) != (row[1], row[3], row[4], row[5], row[6], row[8]) or (
                        row[2] is not None and existing[6] != row[2]
                    ):
                        raise RuntimeError("exit recovery conflicts with existing order identity")
                    continue
                connection.execute(
                    "INSERT INTO paper_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row,
                )
            for row in outbox:
                existing = connection.execute(
                    "SELECT event_type, payload_json FROM paper_outbox WHERE event_key=?",
                    (row[0],),
                ).fetchone()
                if existing is not None:
                    if tuple(existing) != (row[1], row[2]):
                        raise RuntimeError("exit recovery conflicts with existing outbox identity")
                    continue
                connection.execute("INSERT INTO paper_outbox VALUES (?, ?, ?, ?, ?, ?, ?)", row)
            connection.commit()
        return imported

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
        parent_orders: tuple[BrokerOrder, ...] = (),
    ) -> None:
        """Validate supplied snapshots; callers fetch persisted entry parents read-only.

        Parent proof only extends order ownership. It does not authorize entries,
        reconcile net fills, bind broker IDs or change persisted state.
        """
        states = self.load_symbol_states(trade_date)
        owned_clients = {
            str(value)
            for state in states.values()
            for key, value in state.items()
            if key.endswith("_client_id") and isinstance(value, str)
        }
        persisted = {
            order.client_order_id: order for order in self.list_orders()
            if order.trade_date == trade_date
        }
        owned_clients.update(persisted)
        proofs: dict[str, tuple[str, str, str | None, int, str | None]] = {}
        broker_clients: dict[str, str] = {}
        for parent in parent_orders:
            saved = persisted.get(parent.client_order_id)
            if (
                saved is None or saved.role != "entry" or parent.side != "buy"
                or saved.payload.get("side", "buy") != "buy"
                or parent.symbol != saved.symbol or parent.qty != saved.quantity
                or (saved.broker_order_id is not None and parent.id != saved.broker_order_id)
            ):
                raise UnknownBrokerStateError(
                    "parent order identity disagrees with local entry intent"
                )
            for order in (parent, *parent.legs):
                if order is not parent and (
                    order.symbol != parent.symbol or order.side != "sell" or order.qty > parent.qty
                    or order.id == parent.id or order.client_order_id == parent.client_order_id
                ):
                    raise UnknownBrokerStateError(
                        "child order identity disagrees with parent proof"
                    )
                local = persisted.get(order.client_order_id)
                if local is not None and (
                    local.symbol != order.symbol or local.quantity != order.qty
                    or order.side != ("buy" if local.role == "entry" else "sell")
                    or (local.broker_order_id is not None and local.broker_order_id != order.id)
                ):
                    raise UnknownBrokerStateError(
                        "parent proof conflicts with persisted order identity"
                    )
                identity = (order.id, order.symbol, order.side, order.qty, order.order_type)
                if (
                    not order.id or not order.client_order_id
                    or proofs.get(order.client_order_id, identity) != identity
                    or broker_clients.get(order.id, order.client_order_id) != order.client_order_id
                ):
                    raise UnknownBrokerStateError("conflicting parent or child order identity")
                proofs[order.client_order_id] = identity
                broker_clients[order.id] = order.client_order_id
        owned_clients.update(proofs)
        foreign_orders: list[str] = []
        pending = list(reversed(open_orders))
        while pending:
            order = pending.pop()
            identity = (order.id, order.symbol, order.side, order.qty, order.order_type)
            if (
                order.client_order_id not in owned_clients
                or proofs.get(order.client_order_id, identity) != identity
                or broker_clients.get(order.id, order.client_order_id) != order.client_order_id
            ):
                foreign_orders.append(order.client_order_id)
            if parent_orders:
                pending.extend(reversed(order.legs))
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


def _stored_order(row: tuple[object, ...]) -> StoredPaperOrder:
    return StoredPaperOrder(
        trade_date=date.fromisoformat(str(row[0])),
        client_order_id=str(row[1]),
        broker_order_id=None if row[2] is None else str(row[2]),
        symbol=str(row[3]),
        attempt=int(str(row[4])),
        role=str(row[5]),
        quantity=int(str(row[6])),
        status=str(row[7]),
        payload=_decode_object(str(row[8])),
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
