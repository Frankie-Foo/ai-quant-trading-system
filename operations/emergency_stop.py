"""Durable one-way emergency stop for the local Paper session."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class EmergencyStopState:
    active: bool
    activated_at_utc: datetime | None
    reason: str | None


class EmergencyStopStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS emergency_stop (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    active INTEGER NOT NULL CHECK(active IN (0, 1)),
                    activated_at_utc TEXT,
                    reason TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO emergency_stop (
                    singleton, active, activated_at_utc, reason
                ) VALUES (1, 0, NULL, NULL)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def read(self) -> EmergencyStopState:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT active, activated_at_utc, reason
                FROM emergency_stop WHERE singleton=1
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("emergency-stop state disappeared")
        return EmergencyStopState(
            active=bool(row[0]),
            activated_at_utc=(
                datetime.fromisoformat(str(row[1])) if row[1] is not None else None
            ),
            reason=str(row[2]) if row[2] is not None else None,
        )

    def activate(self, *, at_utc: datetime, reason: str) -> EmergencyStopState:
        _require_utc(at_utc)
        if not reason.strip():
            raise ValueError("emergency-stop reason is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT active FROM emergency_stop WHERE singleton=1"
            ).fetchone()
            if current is None:
                raise RuntimeError("emergency-stop state disappeared")
            if not bool(current[0]):
                connection.execute(
                    """
                    UPDATE emergency_stop
                    SET active=1, activated_at_utc=?, reason=?
                    WHERE singleton=1
                    """,
                    (at_utc.isoformat(), reason),
                )
        return self.read()


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("emergency-stop timestamp must be UTC")
