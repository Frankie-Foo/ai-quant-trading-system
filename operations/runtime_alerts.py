"""Persistent first/escalation/recovery alerts for bounded runtime repair."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypeVar

T = TypeVar("T")


class PushPort(Protocol):
    def push(self, body: str) -> str: ...


class RuntimeAlertManager:
    def __init__(self, path: Path, *, push: PushPort):
        self.path = path
        self.push = push
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_faults (
                    fault_key TEXT PRIMARY KEY,
                    episode INTEGER NOT NULL,
                    failures INTEGER NOT NULL,
                    active INTEGER NOT NULL,
                    frozen INTEGER NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_alert_delivery (
                    event_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    message_id TEXT,
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

    def report_failure(
        self,
        fault_key: str,
        *,
        component: str,
        error_type: str,
        observed_at_utc: datetime,
    ) -> None:
        _validate_identity(fault_key, component, observed_at_utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT episode, failures, frozen FROM runtime_faults WHERE fault_key=?",
                (fault_key,),
            ).fetchone()
            episode = int(row[0]) if row is not None else 1
            failures = (int(row[1]) if row is not None else 0) + 1
            frozen = bool(row[2]) if row is not None else False
            frozen = frozen or failures >= 3
            connection.execute(
                """
                INSERT INTO runtime_faults (
                    fault_key, episode, failures, active, frozen, updated_at_utc
                ) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(fault_key) DO UPDATE SET
                    episode=excluded.episode,
                    failures=excluded.failures,
                    active=1,
                    frozen=excluded.frozen,
                    updated_at_utc=excluded.updated_at_utc
                """,
                (
                    fault_key,
                    episode,
                    failures,
                    int(frozen),
                    observed_at_utc.isoformat(),
                ),
            )
            connection.commit()
        if failures == 1:
            self._send_once(
                f"{fault_key}:{episode}:first",
                (
                    f"【AI量化运行报警｜首次故障】{component}\n"
                    f"错误类型：{error_type}；已阻断新开仓并立即执行有界恢复。"
                ),
                observed_at_utc,
            )
        elif failures == 3:
            self._send_once(
                f"{fault_key}:{episode}:escalated",
                (
                    f"【AI量化运行报警｜连续第3次】{component}\n"
                    f"错误类型：{error_type}；运行已冻结，现有持仓继续执行保护退出。"
                ),
                observed_at_utc,
            )

    def report_recovery(
        self,
        fault_key: str,
        *,
        component: str,
        observed_at_utc: datetime,
    ) -> None:
        _validate_identity(fault_key, component, observed_at_utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT episode, active FROM runtime_faults WHERE fault_key=?",
                (fault_key,),
            ).fetchone()
            if row is None or not bool(row[1]):
                connection.rollback()
                return
            episode = int(row[0])
            connection.execute(
                """
                UPDATE runtime_faults
                SET failures=0, active=0, episode=episode+1, updated_at_utc=?
                WHERE fault_key=?
                """,
                (observed_at_utc.isoformat(), fault_key),
            )
            connection.commit()
        self._send_once(
            f"{fault_key}:{episode}:recovered",
            f"【AI量化运行恢复】{component}\n状态：已恢复；冻结状态不自动解除。",
            observed_at_utc,
        )

    def is_frozen(self, fault_key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT frozen FROM runtime_faults WHERE fault_key=?",
                (fault_key,),
            ).fetchone()
        return row is not None and bool(row[0])

    def _send_once(
        self,
        event_key: str,
        body: str,
        observed_at_utc: datetime,
    ) -> None:
        _validate_utf8(body)
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO runtime_alert_delivery (
                        event_key, status, updated_at_utc
                    ) VALUES (?, 'sending', ?)
                    """,
                    (event_key, observed_at_utc.isoformat()),
                )
            except sqlite3.IntegrityError:
                return
        message_id = self.push.push(body)
        if not message_id.strip():
            raise RuntimeError("runtime alert push returned no message ID")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE runtime_alert_delivery
                SET status='sent', message_id=?, updated_at_utc=?
                WHERE event_key=?
                """,
                (message_id, observed_at_utc.isoformat(), event_key),
            )


def bounded_retry(
    action: Callable[[], T],
    *,
    sleep: Callable[[float], None] = time.sleep,
    delays: tuple[float, ...] = (0.25, 0.75),
) -> T:
    """Retry a reconnect/read/write operation at most three times."""
    for attempt in range(len(delays) + 1):
        try:
            return action()
        except Exception:
            if attempt == len(delays):
                raise
            sleep(delays[attempt])
    raise AssertionError("bounded retry loop is unreachable")


def _validate_identity(
    fault_key: str,
    component: str,
    observed_at_utc: datetime,
) -> None:
    if not fault_key.strip() or not component.strip():
        raise ValueError("runtime fault identity is required")
    if observed_at_utc.tzinfo is None or observed_at_utc.utcoffset() != UTC.utcoffset(
        observed_at_utc
    ):
        raise ValueError("runtime alert timestamp must be UTC")


def _validate_utf8(body: str) -> None:
    if "\ufffd" in body or "??" in body:
        raise ValueError("runtime alert contains invalid UTF-8 text")
    if body.encode("utf-8").decode("utf-8") != body:
        raise ValueError("runtime alert is not UTF-8 reversible")
