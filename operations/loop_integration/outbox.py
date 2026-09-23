from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class OutboxItem:
    event_id: str
    event_type: str
    payload: dict[str, Any]
    payload_sha256: str
    status: str
    attempts: int
    remote_task_id: str | None
    remote_run_id: str | None
    failed_node: str | None
    last_error_code: str | None
    retry_after_utc: str | None = None
    retry_count: int = 0


class LoopOutbox:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS loop_outbox (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    remote_task_id TEXT,
                    remote_run_id TEXT,
                    last_error_code TEXT,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
                """
            )
            columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(loop_outbox)")
            }
            if "failed_node" not in columns:
                connection.execute("ALTER TABLE loop_outbox ADD COLUMN failed_node TEXT")
            if "retry_after_utc" not in columns:
                connection.execute("ALTER TABLE loop_outbox ADD COLUMN retry_after_utc TEXT")
            if "retry_count" not in columns:
                connection.execute(
                    "ALTER TABLE loop_outbox ADD COLUMN retry_count INTEGER DEFAULT 0"
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def stage(
        self,
        *,
        event_id: str,
        event_type: Literal["daily_review", "outcome", "event_review"],
        payload: dict[str, Any],
        payload_sha256: str,
    ) -> OutboxItem:
        now = datetime.now(UTC).isoformat()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM loop_outbox WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is not None:
                if str(row["payload_sha256"]) != payload_sha256:
                    raise ValueError("Loop event identity collided with different content")
                return self._item(row)
            connection.execute(
                """
                INSERT INTO loop_outbox (
                    event_id, event_type, payload_json, payload_sha256, status,
                    attempts, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (event_id, event_type, encoded, payload_sha256, now, now),
            )
        item = self.get(event_id)
        if item is None:
            raise RuntimeError("staged Loop outbox item disappeared")
        return item

    def pending(self, *, limit: int = 100) -> tuple[OutboxItem, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM loop_outbox
                WHERE status IN ('pending', 'failed')
                ORDER BY created_at_utc, event_id LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(self._item(row) for row in rows)

    def get(self, event_id: str) -> OutboxItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM loop_outbox WHERE event_id=?", (event_id,)
            ).fetchone()
        return None if row is None else self._item(row)

    def submitted_review(self, trade_date: date) -> OutboxItem | None:
        """Find existing daily work before loading current policy, snapshots or contracts."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM loop_outbox WHERE event_type='daily_review' "
                "AND json_extract(payload_json,'$.trading_date')=? "
                "AND (remote_task_id IS NOT NULL OR status IN ('creating_task','starting_run')) "
                "ORDER BY created_at_utc DESC LIMIT 1", (trade_date.isoformat(),),
            ).fetchone()
        return None if row is None else self._item(row)

    def defer_retry(self, event_id: str, error_code: str, *, now: datetime) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT retry_count,status FROM loop_outbox WHERE event_id=?", (event_id,),
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            if row[1] in {"delivered", "remote_completed", "remote_rejected"}:
                return
            count = int(row[0])
            due = now + timedelta(minutes=(1, 5, 15, 60)[min(count, 3)])
            connection.execute(
                "UPDATE loop_outbox SET retry_count=retry_count+1,retry_after_utc=?,"
                "last_error_code=?,updated_at_utc=? WHERE event_id=?",
                (due.isoformat(), error_code[:128], now.isoformat(), event_id),
            )

    def recoverable_reviews(self, *, now: datetime, limit: int = 5) -> tuple[OutboxItem, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM loop_outbox WHERE event_type='daily_review' "
                "AND remote_task_id IS NOT NULL AND status IN ('remote_processing','task_created',"
                "'failed') AND (retry_after_utc IS NULL OR retry_after_utc<=?) "
                "ORDER BY COALESCE(retry_after_utc,created_at_utc) LIMIT ?",
                (now.isoformat(), limit),
            ).fetchall()
        return tuple(self._item(row) for row in rows)

    def mark_remote_rejected(self, event_id: str, *, error_code: str) -> None:
        self._finish(event_id, status="remote_rejected", error_code=error_code,
                     remote_task_id=None, remote_run_id=None)

    def checkpoint(
        self, event_id: str, status: str, task_id: str | None, run_id: str | None,
    ) -> None:
        """Durable compare-and-set before either remote POST; no duplicate concurrent submit."""
        predecessors = {
            "creating_task": ("pending", "failed", "blocked_precondition", "audit_only_backfill"),
            "task_created": ("creating_task",),
            "starting_run": ("task_created",),
            "remote_processing": ("starting_run",),
        }
        allowed = predecessors[status]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if status == "creating_task":
                conflict = connection.execute(
                    "SELECT 1 FROM loop_outbox WHERE event_id!=? AND event_type='daily_review' "
                    "AND json_extract(payload_json,'$.trading_date')=(SELECT "
                    "json_extract(payload_json,'$.trading_date') FROM loop_outbox "
                    "WHERE event_id=?) AND (remote_task_id IS NOT NULL "
                    "OR status IN ('creating_task','starting_run'))",
                    (event_id, event_id),
                ).fetchone()
                if conflict:
                    raise RuntimeError("Loop daily review already claimed with different evidence")
            cursor = connection.execute(
                "UPDATE loop_outbox SET status=?,remote_task_id=?,remote_run_id=?,"
                "attempts=attempts+1,updated_at_utc=?,last_error_code=NULL "
                f"WHERE event_id=? AND status IN ({','.join('?' for _ in allowed)}) "
                "AND remote_run_id IS NULL "
                + ("AND remote_task_id IS NULL" if status == "creating_task" else ""),
                (status, task_id, run_id, datetime.now(UTC).isoformat(), event_id, *allowed),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Loop submission already claimed; reconcile original receipt")

    def record_error(self, event_id: str, error_code: str) -> None:
        """Keep remote/ambiguous state intact so retry cannot create a new task."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE loop_outbox SET last_error_code=?,updated_at_utc=? WHERE event_id=?",
                (error_code[:128], datetime.now(UTC).isoformat(), event_id),
            )

    def mark_delivered(
        self,
        event_id: str,
        *,
        remote_task_id: str | None = None,
        remote_run_id: str | None = None,
    ) -> None:
        self._finish(
            event_id,
            status="delivered",
            error_code=None,
            remote_task_id=remote_task_id,
            remote_run_id=remote_run_id,
        )

    def mark_remote_processing(
        self,
        event_id: str,
        *,
        remote_task_id: str,
        remote_run_id: str,
    ) -> None:
        """Persist remote acceptance without claiming the remote workflow completed."""
        if not remote_task_id or not remote_run_id:
            raise ValueError("remote processing requires task and run identifiers")
        self._finish(
            event_id,
            status="remote_processing",
            error_code=None,
            remote_task_id=remote_task_id,
            remote_run_id=remote_run_id,
        )

    def mark_remote_completed(
        self,
        event_id: str,
        *,
        remote_task_id: str,
        remote_run_id: str,
    ) -> None:
        """Record a fetched remote terminal success receipt."""
        if not remote_task_id or not remote_run_id:
            raise ValueError("remote completion requires task and run identifiers")
        self._finish(
            event_id,
            status="remote_completed",
            error_code=None,
            remote_task_id=remote_task_id,
            remote_run_id=remote_run_id,
        )

    def mark_failed(
        self,
        event_id: str,
        *,
        error_code: str,
        remote_task_id: str | None = None,
        remote_run_id: str | None = None,
        failed_node: str | None = None,
    ) -> None:
        self._finish(
            event_id,
            status="failed",
            error_code=error_code[:128],
            remote_task_id=remote_task_id,
            remote_run_id=remote_run_id,
            failed_node=failed_node,
        )

    def mark_blocked_precondition(self, event_id: str, *, error_code: str) -> None:
        self._mark_local_block(event_id, "blocked_precondition", error_code)

    def mark_audit_only_backfill(self, event_id: str, *, error_code: str) -> None:
        self._mark_local_block(event_id, "audit_only_backfill", error_code)

    def _mark_local_block(self, event_id: str, status: str, error: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE loop_outbox SET status=?,last_error_code=?,updated_at_utc=? "
                "WHERE event_id=? AND remote_task_id IS NULL AND remote_run_id IS NULL "
                "AND status IN ('pending','failed','blocked_precondition','audit_only_backfill')",
                (status, error[:128], datetime.now(UTC).isoformat(), event_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("cannot replace a claimed Loop submission with local rejection")

    def _finish(
        self,
        event_id: str,
        *,
        status: str,
        error_code: str | None,
        remote_task_id: str | None,
        remote_run_id: str | None,
        failed_node: str | None = None,
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE loop_outbox
                SET status=?, attempts=attempts+1, remote_task_id=COALESCE(?, remote_task_id),
                    remote_run_id=COALESCE(?, remote_run_id), failed_node=?, last_error_code=?,
                    updated_at_utc=? WHERE event_id=?
                    AND status NOT IN ('delivered','remote_completed','remote_rejected')
                    AND (? IS NULL OR remote_task_id IS NULL OR remote_task_id=?)
                    AND (? IS NULL OR remote_run_id IS NULL OR remote_run_id=?)
                """,
                (
                    status,
                    remote_task_id,
                    remote_run_id,
                    failed_node,
                    error_code,
                    datetime.now(UTC).isoformat(),
                    event_id,
                    remote_task_id, remote_task_id, remote_run_id, remote_run_id,
                ),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT status FROM loop_outbox WHERE event_id=?", (event_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(event_id)
                if row[0] not in {"delivered", "remote_completed", "remote_rejected"}:
                    raise RuntimeError("Loop remote receipt identity changed")

    @staticmethod
    def _item(row: sqlite3.Row) -> OutboxItem:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise ValueError("Loop outbox payload is not an object")
        return OutboxItem(
            event_id=str(row["event_id"]),
            event_type=str(row["event_type"]),
            payload=payload,
            payload_sha256=str(row["payload_sha256"]),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            remote_task_id=(None if row["remote_task_id"] is None else str(row["remote_task_id"])),
            remote_run_id=(None if row["remote_run_id"] is None else str(row["remote_run_id"])),
            failed_node=(None if row["failed_node"] is None else str(row["failed_node"])),
            last_error_code=(
                None if row["last_error_code"] is None else str(row["last_error_code"])
            ),
            retry_after_utc=row["retry_after_utc"],
            retry_count=int(row["retry_count"] or 0),
        )
