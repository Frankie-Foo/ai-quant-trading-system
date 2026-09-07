from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
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
        event_type: Literal["daily_review", "outcome"],
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
        self._finish(
            event_id,
            status="blocked_precondition",
            error_code=error_code[:128],
            remote_task_id=None,
            remote_run_id=None,
            failed_node=None,
        )

    def mark_audit_only_backfill(self, event_id: str, *, error_code: str) -> None:
        self._finish(
            event_id,
            status="audit_only_backfill",
            error_code=error_code[:128],
            remote_task_id=None,
            remote_run_id=None,
            failed_node=None,
        )

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
                """,
                (
                    status,
                    remote_task_id,
                    remote_run_id,
                    failed_node,
                    error_code,
                    datetime.now(UTC).isoformat(),
                    event_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(event_id)

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
        )
