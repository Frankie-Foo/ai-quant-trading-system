"""Durable audit ledger for complete Alpaca Paper sessions."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path


class PaperSessionStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    BOUNDED_SMOKE = "bounded_smoke"
    FAILED = "failed"


@dataclass(frozen=True)
class PaperSessionRecord:
    trade_date: date
    started_at_utc: datetime
    expected_close_utc: datetime
    ended_at_utc: datetime | None
    status: PaperSessionStatus
    event_count: int
    orders_submitted: int
    reconciliation_match_rate: float
    error_type: str | None


class PaperSessionLedger:
    """One authoritative record per XNYS session, restart safe by trade date."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @staticmethod
    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("session timestamps must be timezone-aware UTC")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_sessions (
                    trade_date TEXT PRIMARY KEY,
                    started_at_utc TEXT NOT NULL,
                    expected_close_utc TEXT NOT NULL,
                    ended_at_utc TEXT,
                    status TEXT NOT NULL,
                    event_count INTEGER NOT NULL CHECK(event_count >= 0),
                    orders_submitted INTEGER NOT NULL CHECK(orders_submitted >= 0),
                    reconciliation_match_rate REAL NOT NULL
                        CHECK(reconciliation_match_rate >= 0
                              AND reconciliation_match_rate <= 1),
                    error_type TEXT
                )
                """
            )

    def start(
        self,
        *,
        trade_date: date,
        started_at_utc: datetime,
        expected_close_utc: datetime,
        reconciliation_match_rate: float,
    ) -> None:
        self._require_utc(started_at_utc)
        self._require_utc(expected_close_utc)
        if not 0 <= reconciliation_match_rate <= 1:
            raise ValueError("reconciliation_match_rate must be between zero and one")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT status FROM paper_sessions WHERE trade_date = ?",
                (trade_date.isoformat(),),
            ).fetchone()
            if existing is not None and str(existing[0]) == PaperSessionStatus.COMPLETED:
                raise RuntimeError("completed Paper session cannot be restarted")
            connection.execute(
                """
                INSERT INTO paper_sessions (
                    trade_date, started_at_utc, expected_close_utc, ended_at_utc,
                    status, event_count, orders_submitted,
                    reconciliation_match_rate, error_type
                ) VALUES (?, ?, ?, NULL, ?, 0, 0, ?, NULL)
                ON CONFLICT(trade_date) DO UPDATE SET
                    started_at_utc = excluded.started_at_utc,
                    expected_close_utc = excluded.expected_close_utc,
                    ended_at_utc = NULL,
                    status = excluded.status,
                    event_count = 0,
                    orders_submitted = 0,
                    reconciliation_match_rate = excluded.reconciliation_match_rate,
                    error_type = NULL
                """,
                (
                    trade_date.isoformat(),
                    started_at_utc.isoformat(),
                    expected_close_utc.isoformat(),
                    PaperSessionStatus.RUNNING.value,
                    reconciliation_match_rate,
                ),
            )

    def finish(
        self,
        *,
        trade_date: date,
        ended_at_utc: datetime,
        status: PaperSessionStatus,
        event_count: int,
        orders_submitted: int,
        error_type: str | None = None,
    ) -> None:
        self._require_utc(ended_at_utc)
        if status is PaperSessionStatus.RUNNING:
            raise ValueError("finish status cannot be running")
        if event_count < 0 or orders_submitted < 0:
            raise ValueError("session counters cannot be negative")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE paper_sessions
                SET ended_at_utc = ?, status = ?, event_count = ?,
                    orders_submitted = ?, error_type = ?
                WHERE trade_date = ? AND status = ?
                """,
                (
                    ended_at_utc.isoformat(),
                    status.value,
                    event_count,
                    orders_submitted,
                    error_type,
                    trade_date.isoformat(),
                    PaperSessionStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Paper session is not in running state")

    def records(self) -> tuple[PaperSessionRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date, started_at_utc, expected_close_utc, ended_at_utc,
                       status, event_count, orders_submitted,
                       reconciliation_match_rate, error_type
                FROM paper_sessions ORDER BY trade_date
                """
            ).fetchall()
        return tuple(
            PaperSessionRecord(
                trade_date=date.fromisoformat(str(row[0])),
                started_at_utc=datetime.fromisoformat(str(row[1])),
                expected_close_utc=datetime.fromisoformat(str(row[2])),
                ended_at_utc=(
                    None if row[3] is None else datetime.fromisoformat(str(row[3]))
                ),
                status=PaperSessionStatus(str(row[4])),
                event_count=int(row[5]),
                orders_submitted=int(row[6]),
                reconciliation_match_rate=float(row[7]),
                error_type=None if row[8] is None else str(row[8]),
            )
            for row in rows
        )

