"""Idempotent daily ingestion, catalyst lock, RVOL, and final-selection scheduler."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from data_plane.calendar import build_xnys_schedule
from kernel.config import load_config
from operations.local_env import load_project_env, project_data_root
from schedule.runtime import JsonEventLogger, LockUnavailableError, ProcessLock
from schedule.state import JobLedger, JobStatus

ROOT = Path(__file__).resolve().parents[1]
BEIJING = ZoneInfo("Asia/Shanghai")
LOCK_JOB = "premarket_catalyst_lock"
LOCK_VERSION = "premarket_catalyst_lock.v5"
SELECTION_JOB = "premarket_final_selection"
SELECTION_VERSION = "premarket_final_selection.v4"
SHADOW_JOB = "premarket_multisignal_shadow"
SHADOW_VERSION = "premarket_multisignal_shadow.v1"


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def phase_times(trade_date: date) -> tuple[datetime, datetime]:
    cfg = load_config(ROOT / "config.yaml")
    lock_time = datetime.strptime(cfg.guardrails.lock_time_beijing, "%H:%M").time()
    selection_time = datetime.strptime(
        cfg.guardrails.selection_time_beijing, "%H:%M"
    ).time()
    lock = datetime.combine(trade_date, lock_time, BEIJING).astimezone(UTC)
    selection = datetime.combine(trade_date, selection_time, BEIJING).astimezone(UTC)
    return lock, selection


def target_for_tick(now_utc: datetime) -> date | None:
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    local_date = now_utc.astimezone(BEIJING).date()
    schedule = build_xnys_schedule(local_date - timedelta(days=3), local_date + timedelta(days=7))
    candidates = schedule["trade_date"].to_list()
    due = [value for value in candidates if phase_times(value)[0] <= now_utc]
    return max(due) if due else None


def _extract_artifacts(stdout: str) -> tuple[str, ...]:
    values: set[str] = set()
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            value = payload.get("dataset_id")
            if isinstance(value, str):
                values.add(value)
    values.update(re.findall(r'"dataset_id"\s*:\s*"([^"]+)"', stdout))
    return tuple(sorted(values))


def _run(
    arguments: list[str], *, logger: JsonEventLogger, timeout: int = 3600
) -> tuple[str, ...]:
    started = time.monotonic()
    logger.emit("child_started", command=arguments[:3])
    completed = subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    elapsed_ms = round((time.monotonic() - started) * 1000)
    if completed.returncode != 0:
        logger.emit(
            "child_failed",
            level="error",
            command=arguments[:3],
            return_code=completed.returncode,
            stdout_lines=len(completed.stdout.splitlines()),
            stderr_lines=len(completed.stderr.splitlines()),
            elapsed_ms=elapsed_ms,
        )
        raise RuntimeError(f"child failed with exit code {completed.returncode}")
    logger.emit("child_completed", command=arguments[:3], elapsed_ms=elapsed_ms)
    return _extract_artifacts(completed.stdout)


def _previous_session(trade_date: date) -> date:
    schedule = build_xnys_schedule(trade_date - timedelta(days=10), trade_date)
    prior = schedule.filter(schedule["trade_date"] < trade_date)["trade_date"].tail(1)
    if len(prior) != 1:
        raise ValueError("previous XNYS session is unavailable")
    value = prior[0]
    if not isinstance(value, date):
        raise ValueError("previous XNYS session is invalid")
    return value


def _has_reference_snapshot(data_root: Path, asof_date: date) -> bool:
    """Avoid re-downloading an immutable point-in-time reference snapshot."""

    for path in (data_root / "accepted").glob(
        "massive.reference_tickers.cs-*/data.parquet"
    ):
        try:
            value = pl.read_parquet(path, columns=["asof_date"])["asof_date"].max()
        except (OSError, pl.exceptions.PolarsError):
            continue
        if value == asof_date:
            return True
    return False


def _lock_stage(trade_date: date, data_root: Path, logger: JsonEventLogger) -> tuple[str, ...]:
    previous = _previous_session(trade_date)
    artifacts: list[str] = []
    artifacts.extend(
        _run(
            [
                "-m",
                "data_plane.cli",
                "--data-root",
                str(data_root),
                "massive-grouped-daily",
                "--start",
                previous.isoformat(),
                "--end",
                previous.isoformat(),
            ],
            logger=logger,
        )
    )
    if not _has_reference_snapshot(data_root, previous):
        artifacts.extend(
            _run(
                [
                    "-m",
                    "data_plane.cli",
                    "--data-root",
                    str(data_root),
                    "massive-reference",
                    "--date",
                    previous.isoformat(),
                ],
                logger=logger,
            )
        )
    else:
        logger.emit(
            "reference_snapshot_reused",
            asof_date=previous.isoformat(),
        )
    for module in (
        "scripts.build_daily_universe",
        "scripts.build_catalyst_snapshot",
        "scripts.build_premarket_rvol",
        "scripts.build_selection_gates",
    ):
        artifacts.extend(
            _run(
                [
                    "-m",
                    module,
                    "--trade-date",
                    trade_date.isoformat(),
                    "--data-root",
                    str(data_root),
                ],
                logger=logger,
            )
        )
    return tuple(dict.fromkeys(artifacts))


def _selection_stage(
    trade_date: date, data_root: Path, logger: JsonEventLogger
) -> tuple[str, ...]:
    artifacts: list[str] = []
    for module in (
        "scripts.build_premarket_rvol",
        "scripts.build_selection_gates",
    ):
        artifacts.extend(
            _run(
                [
                    "-m",
                    module,
                    "--trade-date",
                    trade_date.isoformat(),
                    "--data-root",
                    str(data_root),
                ],
                logger=logger,
            )
        )
    return tuple(dict.fromkeys(artifacts))


def _shadow_stage(
    trade_date: date, data_root: Path, logger: JsonEventLogger
) -> tuple[str, ...]:
    return _run(
        [
            "-m",
            "scripts.run_multisignal_shadow_pipeline",
            "--trade-date",
            trade_date.isoformat(),
            "--data-root",
            str(data_root),
        ],
        logger=logger,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_parse_date)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    parser.add_argument("--state-db", type=Path, default=ROOT / "runs/jobs.sqlite3")
    parser.add_argument("--lock-file", type=Path, default=ROOT / "runs/premarket.lock")
    return parser


def run(argv: list[str] | None = None, *, now_utc: datetime | None = None) -> int:
    load_project_env(ROOT)
    args = _parser().parse_args(argv)
    logger = JsonEventLogger(service="premarket")
    try:
        with ProcessLock(args.lock_file):
            return _run_locked(args, logger, now_utc=now_utc or datetime.now(UTC))
    except LockUnavailableError:
        logger.emit("tick_skipped_lock_held", level="warning")
        return 0


def _run_locked(
    args: argparse.Namespace, logger: JsonEventLogger, *, now_utc: datetime
) -> int:
    cfg = load_config(ROOT / "config.yaml")
    trade_date = args.trade_date or target_for_tick(now_utc)
    if trade_date is None:
        logger.emit("tick_no_due_session")
        return 0
    lock_due, selection_due = phase_times(trade_date)
    # The outer process lock proves that no healthy peer is running. A prior
    # RUNNING lease is therefore an abruptly terminated process and is safe to
    # recover immediately instead of suppressing today's selection for hours.
    ledger = JobLedger(args.state_db, stale_after=timedelta(0))
    logger.emit(
        "tick_started",
        trade_date=trade_date.isoformat(),
        lock_due=now_utc >= lock_due,
        selection_due=now_utc >= selection_due,
        orders_submitted=0,
    )
    if now_utc < lock_due:
        return 0

    lock_record = ledger.get(LOCK_JOB, trade_date, LOCK_VERSION)
    if lock_record is None or lock_record.status is not JobStatus.SUCCEEDED:
        lease = ledger.acquire(
            LOCK_JOB,
            trade_date,
            LOCK_VERSION,
            max_attempts=cfg.scheduler.premarket_max_attempts,
            retry_after=timedelta(minutes=cfg.scheduler.premarket_retry_minutes),
        )
        if lease is None:
            record = ledger.get(LOCK_JOB, trade_date, LOCK_VERSION)
            exhausted = (
                record is not None
                and record.attempts >= cfg.scheduler.premarket_max_attempts
            )
            logger.emit(
                "lock_stage_exhausted" if exhausted else "lock_stage_pending_retry",
                level="error" if exhausted else "warning",
                status=record.status.value if record is not None else "missing",
                attempts=record.attempts if record is not None else 0,
            )
            return 1 if exhausted else 0
        try:
            artifacts = _lock_stage(trade_date, args.data_root, logger)
            ledger.complete(lease, artifact_ids=artifacts)
        except Exception as exc:
            ledger.fail(lease, error_code=type(exc).__name__)
            logger.emit("lock_stage_failed", level="error", error_code=type(exc).__name__)
            return 1

    if now_utc < selection_due:
        logger.emit("tick_completed_history_prefetched", orders_submitted=0)
        return 0
    lease = ledger.acquire(
        SELECTION_JOB,
        trade_date,
        SELECTION_VERSION,
        max_attempts=cfg.scheduler.premarket_max_attempts,
        retry_after=timedelta(minutes=cfg.scheduler.premarket_retry_minutes),
    )
    if lease is not None:
        try:
            artifacts = _selection_stage(trade_date, args.data_root, logger)
            ledger.complete(lease, artifact_ids=artifacts)
        except Exception as exc:
            ledger.fail(lease, error_code=type(exc).__name__)
            logger.emit(
                "selection_stage_failed", level="error", error_code=type(exc).__name__
            )
            return 1
    else:
        record = ledger.get(SELECTION_JOB, trade_date, SELECTION_VERSION)
        if record is None or record.status is not JobStatus.SUCCEEDED:
            exhausted = (
                record is not None
                and record.attempts >= cfg.scheduler.premarket_max_attempts
            )
            logger.emit(
                (
                    "selection_stage_exhausted"
                    if exhausted
                    else "selection_stage_pending_retry"
                ),
                level="error" if exhausted else "warning",
                status=record.status.value if record is not None else "missing",
                attempts=record.attempts if record is not None else 0,
            )
            return 1 if exhausted else 0
    shadow_status = "disabled"
    if cfg.scheduler.multisignal_shadow_enabled:
        shadow_status = "pending"
        shadow_lease = ledger.acquire(
            SHADOW_JOB,
            trade_date,
            SHADOW_VERSION,
            max_attempts=cfg.scheduler.premarket_max_attempts,
            retry_after=timedelta(minutes=cfg.scheduler.premarket_retry_minutes),
        )
        if shadow_lease is not None:
            try:
                shadow_artifacts = _shadow_stage(
                    trade_date,
                    args.data_root,
                    logger,
                )
                ledger.complete(shadow_lease, artifact_ids=shadow_artifacts)
                shadow_status = "complete"
            except Exception as exc:
                ledger.fail(shadow_lease, error_code=type(exc).__name__)
                shadow_status = "failed_retryable"
                logger.emit(
                    "multisignal_shadow_failed",
                    level="warning",
                    error_code=type(exc).__name__,
                    primary_selection_affected=False,
                    orders_submitted=0,
                )
        else:
            shadow_record = ledger.get(SHADOW_JOB, trade_date, SHADOW_VERSION)
            if (
                shadow_record is not None
                and shadow_record.status is JobStatus.SUCCEEDED
            ):
                shadow_status = "complete"
            elif (
                shadow_record is not None
                and shadow_record.attempts >= cfg.scheduler.premarket_max_attempts
            ):
                shadow_status = "exhausted"
                logger.emit(
                    "multisignal_shadow_exhausted",
                    level="warning",
                    attempts=shadow_record.attempts,
                    primary_selection_affected=False,
                    orders_submitted=0,
                )
    logger.emit(
        "tick_completed",
        orders_submitted=0,
        multisignal_shadow_status=shadow_status,
    )
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
