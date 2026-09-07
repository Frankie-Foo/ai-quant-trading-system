"""Durable, exchange-time scheduler for the three-stage intraday funnel."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from data_plane.calendar import build_xnys_schedule
from operations.autonomous_selection_handoff import load_open_confirmation
from operations.feishu_base import FeishuCliError

EASTERN = ZoneInfo("America/New_York")
BEIJING = ZoneInfo("Asia/Shanghai")
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
    HANDOFF_PENDING = "handoff_pending"
    MONITORING = "monitoring"
    BLOCKED = "blocked"


class PaperMonitorBlocked(RuntimeError):
    """A known monitor cannot safely be treated as ready or replaced."""


@dataclass(frozen=True)
class FunnelTickResult:
    status: FunnelTickStatus
    stage: FunnelStage | None = None
    detail: str = ""


class FunnelStageExecutor(Protocol):
    def execute(
        self, stage: FunnelStage, trade_date: date, *, resume_only: bool = False
    ) -> dict[str, str]: ...

    def can_resume(self, trade_date: date, *, now_utc: datetime) -> bool: ...


class CompletedStageProcess(Protocol):
    returncode: int
    stdout: str
    stderr: str


StageRunner = Callable[..., CompletedStageProcess]


@dataclass(frozen=True)
class ProductionFunnelExecutor:
    """Run one explicit stage process and accept only a verifiable receipt."""

    root: Path
    runner: StageRunner = subprocess.run

    def can_resume(self, trade_date: date, *, now_utc: datetime) -> bool:
        """Require the frozen authorization and its verified config, not merely a failed row."""
        path = self.root / "runs" / "autonomous" / trade_date.isoformat() / "open_confirmation.json"
        try:
            confirmation = load_open_confirmation(path)
            generated = confirmation.generated_at_utc
            return (
                confirmation.authorization.trade_date == trade_date
                and generated.tzinfo is not None
                and generated.astimezone(EASTERN).date() == trade_date
                and generated <= now_utc
            )
        except (OSError, ValueError, TypeError):
            return False

    def execute(
        self, stage: FunnelStage, trade_date: date, *, resume_only: bool = False
    ) -> dict[str, str]:
        command = [
            sys.executable,
            "-m",
            "scripts.run_modern_funnel_stage",
            "--stage",
            stage.value,
            "--trade-date",
            trade_date.isoformat(),
        ]
        if resume_only:
            command.append("--resume-only")
        try:
            completed = self.runner(
                command,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=3600,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"{stage.value} process failed: {type(exc).__name__}") from None
        if completed.returncode != 0:
            stderr_lines = [line.strip() for line in completed.stderr.splitlines() if line.strip()]
            detail = _safe_process_error(stderr_lines[-1]) if stderr_lines else ""
            if detail.startswith("PaperMonitorBlocked: "):
                raise PaperMonitorBlocked(detail.removeprefix("PaperMonitorBlocked: "))
            raise RuntimeError(
                f"{stage.value} process failed with exit code {completed.returncode}: {detail}"
            )
        receipt = _last_json_object(completed.stdout)
        if receipt.get("ok") is not True or not str(receipt.get("receipt_id", "")).strip():
            raise RuntimeError(f"{stage.value} did not produce a success receipt")
        return {
            str(key): str(value)
            for key, value in receipt.items()
            if isinstance(value, (str, int, float, bool))
        }


def _safe_process_error(line: str) -> str:
    blocked = re.fullmatch(
        r"(?:schedule.modern_funnel\.)?PaperMonitorBlocked: "
        r"(live_monitor_lease_expired|monitor_exited_with_active_lease|"
        r"monitor_identity_mismatch|monitor_identity_unverified)",
        line,
    )
    if blocked:
        return f"PaperMonitorBlocked: {blocked.group(1)}"
    match = re.fullmatch(
        r"(?:operations.feishu_base\.)?FeishuCliError: lark-cli failed "
        r"\(type=([a-z_]+), code=([0-9]{1,10}|unknown), exit_code=(-?[0-9]+|unknown)\)",
        line,
    )
    if match:
        kind, code, exit_code = match.groups()
        return str(FeishuCliError(kind, code, None if exit_code == "unknown" else int(exit_code)))
    if line == "RuntimeError: Paper startup failed":
        return line
    return "stage error details redacted; inspect local logs"


def _last_json_object(stdout: str) -> dict[str, object]:
    if len(stdout) > 2_000_000:
        raise RuntimeError("funnel stage receipt exceeds output limit")
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(value, dict):
            return cast(dict[str, object], value)
    raise RuntimeError("funnel stage produced no JSON receipt")


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


def _session_close_utc(trade_date: date) -> datetime | None:
    session = build_xnys_schedule(trade_date, trade_date)
    return None if session.is_empty() else cast(datetime, session["market_close_utc"][0])


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
        "SELECT status, lease_until_utc, receipt_json FROM funnel_runs "
        "WHERE trade_date = ? AND stage = ?",
        (day, stage.value),
    ).fetchone()
    supervised = (
        row is not None and stage is FunnelStage.OPEN_CONFIRMATION and _has_candidates(row[2])
    )
    if row is not None and row[0] == FunnelTickStatus.SUCCEEDED.value and not supervised:
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
        SET status = ?, lease_until_utc = NULL, receipt_json = COALESCE(?, receipt_json), error = ?,
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


def _has_candidates(receipt_json: str | None) -> bool:
    if not receipt_json:
        return False
    receipt = json.loads(receipt_json)
    return isinstance(receipt, dict) and bool(receipt.get("symbols"))


def run_tick(
    *,
    ledger_path: Path,
    executor: FunnelStageExecutor,
    now_utc: datetime | None = None,
    first_wave_not_before_beijing: time | None = None,
) -> FunnelTickResult:
    """Publish once; supervise eligible execution without claiming trading success."""
    current = now_utc or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    current = current.astimezone(UTC)
    eastern = current.astimezone(EASTERN)
    trade_date = eastern.date()
    session_close = _session_close_utc(trade_date)
    if session_close is None:
        return FunnelTickResult(FunnelTickStatus.NOT_TRADING_DAY)

    stage = _stage_for(eastern.time().replace(tzinfo=None))
    if (
        stage is FunnelStage.FIRST_WAVE
        and first_wave_not_before_beijing is not None
        and current.astimezone(BEIJING).time() < first_wave_not_before_beijing
    ):
        return FunnelTickResult(FunnelTickStatus.NOT_DUE, stage)
    resume_only = stage is None and time(9, 45) <= eastern.time() and current < session_close
    if stage is None and not resume_only:
        return FunnelTickResult(FunnelTickStatus.NOT_DUE)

    with _connect(ledger_path) as connection:
        if resume_only:
            stage = FunnelStage.OPEN_CONFIRMATION
            existing = connection.execute(
                "SELECT status, receipt_json FROM funnel_runs WHERE trade_date=? AND stage=?",
                (trade_date.isoformat(), stage.value),
            ).fetchone()
            # Never invent a missing stage or retry a completed no-trade selection.
            if existing is None or (
                existing[0] == FunnelTickStatus.SUCCEEDED.value and not _has_candidates(existing[1])
            ):
                return FunnelTickResult(FunnelTickStatus.NOT_DUE)
            if not executor.can_resume(trade_date, now_utc=current):
                # Preserve attempts, original error and receipt when there is nothing to recover.
                return FunnelTickResult(FunnelTickStatus.NOT_DUE)
        assert stage is not None
        claim_status = _claim(
            connection,
            trade_date=trade_date,
            stage=stage,
            now_utc=current,
        )
        if claim_status is not None:
            return FunnelTickResult(claim_status, stage)
        try:
            receipt = (
                executor.execute(stage, trade_date, resume_only=True)
                if resume_only
                else executor.execute(stage, trade_date)
            )
        except Exception as exc:
            status = (
                FunnelTickStatus.BLOCKED
                if isinstance(exc, PaperMonitorBlocked)
                else FunnelTickStatus.FAILED
            )
            _finish(
                connection,
                trade_date=trade_date,
                stage=stage,
                now_utc=current,
                status=status,
                error=f"{type(exc).__name__}: {exc}",
            )
            return FunnelTickResult(status, stage, str(exc))
        status = FunnelTickStatus.SUCCEEDED
        if stage is FunnelStage.OPEN_CONFIRMATION and receipt.get("symbols"):
            status = (
                FunnelTickStatus.MONITORING
                if str(receipt.get("paper_started", "")).lower() == "true"
                else FunnelTickStatus.HANDOFF_PENDING
            )
        _finish(
            connection,
            trade_date=trade_date,
            stage=stage,
            now_utc=current,
            status=status,
            receipt=receipt,
        )
        return FunnelTickResult(status, stage)


def _beijing_time(value: str) -> time:
    if re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) is None:
        raise argparse.ArgumentTypeError("time must use HH:MM (00:00..23:59)")
    return time.fromisoformat(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger-path",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "runs" / "modern-funnel.sqlite3",
    )
    parser.add_argument(
        "--first-wave-not-before-beijing",
        type=_beijing_time,
        metavar="HH:MM",
        help="Gate FIRST_WAVE by Beijing wall time; does not change snapshot cutoffs or recovery.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    root = Path(__file__).resolve().parents[1]
    result = run_tick(
        ledger_path=args.ledger_path,
        executor=ProductionFunnelExecutor(root=root),
        first_wave_not_before_beijing=args.first_wave_not_before_beijing,
    )
    print(
        json.dumps(
            {
                "status": result.status.value,
                "stage": result.stage.value if result.stage is not None else None,
                "detail": result.detail,
            },
            sort_keys=True,
        )
    )
    failed = {
        FunnelTickStatus.FAILED,
        FunnelTickStatus.BLOCKED,
        FunnelTickStatus.PREREQUISITE_MISSING,
    }
    return 1 if result.status in failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
