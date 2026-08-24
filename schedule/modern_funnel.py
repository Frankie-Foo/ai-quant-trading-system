"""Durable, exchange-time scheduler for the three-stage intraday funnel."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from data_plane.calendar import build_xnys_schedule

EASTERN = ZoneInfo("America/New_York")
LEASE_DURATION = timedelta(minutes=15)


class FunnelStage(StrEnum):
    FIRST_WAVE = "first_wave"
    SECOND_WAVE = "second_wave"
    OPEN_CONFIRMATION = "open_confirmation"


class FunnelTickStatus(StrEnum):
    SUCCEEDED = "succeeded"
    ALREADY_SUCCEEDED = "already_succeeded"
    FAILED = "failed"
    NOT_DUE = "not_due"
    NOT_TRADING_DAY = "not_trading_day"
    PREREQUISITE_MISSING = "prerequisite_missing"
    LEASED = "leased"


@dataclass(frozen=True)
class FunnelTickResult:
    status: FunnelTickStatus
    stage: FunnelStage | None = None
    detail: str = ""


class FunnelStageExecutor(Protocol):
    def execute(self, stage: FunnelStage, trade_date: date) -> dict[str, str]: ...


def _stage_for(local_time: time) -> FunnelStage | None:
    if time(8) <= local_time < time(9, 25):
        return FunnelStage.FIRST_WAVE
    if time(9, 25) <= local_time < time(9, 30):
        return FunnelStage.SECOND_WAVE
    if time(9, 35) <= local_time < time(9, 45):
        return FunnelStage.OPEN_CONFIRMATION
    return None


def _prerequisite(stage: FunnelStage) -> FunnelStage | None:
    return {
        FunnelStage.FIRST_WAVE: None,
        FunnelStage.SECOND_WAVE: FunnelStage.FIRST_WAVE,
        FunnelStage.OPEN_CONFIRMATION: FunnelStage.SECOND_WAVE,
    }[stage]


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS funnel_runs (
            trade_date TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            lease_until_utc TEXT,
            receipt_json TEXT,
            error TEXT,
            updated_at_utc TEXT NOT NULL,
            PRIMARY KEY (trade_date, stage)
        )
        """
    )
    return connection


def _is_trading_day(trade_date: date) -> bool:
    return not build_xnys_schedule(trade_date, trade_date).is_empty()


def _claim(
    connection: sqlite3.Connection,
    *,
    trade_date: date,
    stage: FunnelStage,
    now_utc: datetime,
) -> FunnelTickStatus | None:
    day = trade_date.isoformat()
    prerequisite = _prerequisite(stage)
    connection.execute("BEGIN IMMEDIATE")
    if prerequisite is not None:
        row = connection.execute(
            "SELECT status FROM funnel_runs WHERE trade_date = ? AND stage = ?",
            (day, prerequisite.value),
        ).fetchone()
        if row is None or row[0] != FunnelTickStatus.SUCCEEDED.value:
            connection.rollback()
            return FunnelTickStatus.PREREQUISITE_MISSING

    row = connection.execute(
        "SELECT status, lease_until_utc FROM funnel_runs "
        "WHERE trade_date = ? AND stage = ?",
        (day, stage.value),
    ).fetchone()
    if row is not None and row[0] == FunnelTickStatus.SUCCEEDED.value:
        connection.rollback()
        return FunnelTickStatus.ALREADY_SUCCEEDED
    if row is not None and row[0] == "running" and row[1]:
        active_lease_until = datetime.fromisoformat(row[1])
        if active_lease_until > now_utc:
            connection.rollback()
            return FunnelTickStatus.LEASED

    updated_at = now_utc.isoformat()
    lease_until_utc = (now_utc + LEASE_DURATION).isoformat()
    connection.execute(
        """
        INSERT INTO funnel_runs (
            trade_date, stage, status, attempts, lease_until_utc, updated_at_utc
        ) VALUES (?, ?, 'running', 1, ?, ?)
        ON CONFLICT(trade_date, stage) DO UPDATE SET
            status = 'running',
            attempts = funnel_runs.attempts + 1,
            lease_until_utc = excluded.lease_until_utc,
            error = NULL,
            updated_at_utc = excluded.updated_at_utc
        """,
        (day, stage.value, lease_until_utc, updated_at),
    )
    connection.commit()
    return None


def _finish(
    connection: sqlite3.Connection,
    *,
    trade_date: date,
    stage: FunnelStage,
    now_utc: datetime,
    status: FunnelTickStatus,
    receipt: dict[str, str] | None = None,
    error: str | None = None,
) -> None:
    connection.execute(
        """
        UPDATE funnel_runs
        SET status = ?, lease_until_utc = NULL, receipt_json = ?, error = ?,
            updated_at_utc = ?
        WHERE trade_date = ? AND stage = ?
        """,
        (
            status.value,
            json.dumps(receipt, sort_keys=True) if receipt is not None else None,
            error,
            now_utc.isoformat(),
            trade_date.isoformat(),
            stage.value,
        ),
    )
    connection.commit()


def run_tick(
    *,
    ledger_path: Path,
    executor: FunnelStageExecutor,
    now_utc: datetime | None = None,
) -> FunnelTickResult:
    """Run at most one due funnel stage, exactly once after a successful receipt."""
    current = now_utc or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    current = current.astimezone(UTC)
    eastern = current.astimezone(EASTERN)
    trade_date = eastern.date()
    if not _is_trading_day(trade_date):
        return FunnelTickResult(FunnelTickStatus.NOT_TRADING_DAY)

    stage = _stage_for(eastern.time().replace(tzinfo=None))
    if stage is None:
        return FunnelTickResult(FunnelTickStatus.NOT_DUE)

    with _connect(ledger_path) as connection:
        claim_status = _claim(
            connection,
            trade_date=trade_date,
            stage=stage,
            now_utc=current,
        )
        if claim_status is not None:
            return FunnelTickResult(claim_status, stage)
        try:
            receipt = executor.execute(stage, trade_date)
        except Exception as exc:
            _finish(
                connection,
                trade_date=trade_date,
                stage=stage,
                now_utc=current,
                status=FunnelTickStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
            return FunnelTickResult(FunnelTickStatus.FAILED, stage, str(exc))
        _finish(
            connection,
            trade_date=trade_date,
            stage=stage,
            now_utc=current,
            status=FunnelTickStatus.SUCCEEDED,
            receipt=receipt,
        )
        return FunnelTickResult(FunnelTickStatus.SUCCEEDED, stage)
