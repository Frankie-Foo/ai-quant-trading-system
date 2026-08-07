"""Idempotent monthly draft-proposal scheduler."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from data_plane.calendar import build_xnys_schedule
from schedule.child_process import run_child
from schedule.runtime import JsonEventLogger, LockUnavailableError, ProcessLock
from schedule.state import JobLedger

ROOT = Path(__file__).resolve().parents[1]
BEIJING = ZoneInfo("Asia/Shanghai")
JOB_NAME = "monthly_evolution"
JOB_VERSION = "monthly_evolution.v1"


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def is_first_xnys_session(value: date) -> bool:
    start = value.replace(day=1)
    end = value.replace(day=8)
    schedule = build_xnys_schedule(start, end)
    if schedule.is_empty():
        return False
    first = schedule.get_column("trade_date").min()
    return first == value


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asof-date", type=_parse_date, default=datetime.now(BEIJING).date())
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--state-db", type=Path, default=ROOT / "runs" / "jobs.sqlite3")
    parser.add_argument("--lock-file", type=Path, default=ROOT / "runs" / "monthly-evolution.lock")
    parser.add_argument("--llm-mode", choices=("off", "optional", "required"), default="optional")
    args = parser.parse_args(argv)
    logger = JsonEventLogger(service="monthly_evolution")
    try:
        with ProcessLock(args.lock_file):
            if not is_first_xnys_session(args.asof_date):
                logger.emit("not_first_xnys_session", asof_date=args.asof_date.isoformat())
                return 0
            ledger = JobLedger(args.state_db)
            lease = ledger.acquire(JOB_NAME, args.asof_date, JOB_VERSION)
            if lease is None:
                logger.emit("job_skipped", asof_date=args.asof_date.isoformat())
                return 0
            result = run_child(
                [
                    sys.executable,
                    "-m",
                    "scripts.run_monthly_evolution",
                    "--asof-date",
                    args.asof_date.isoformat(),
                    "--data-root",
                    str(args.data_root),
                    "--llm-mode",
                    args.llm_mode,
                ],
                cwd=ROOT,
                timeout_seconds=1800,
            )
            if result.return_code != 0:
                ledger.fail(lease, error_code="MonthlyEvolutionFailed")
                logger.emit("job_failed", level="error", orders_submitted=0)
                return 1
            payload = json.loads(result.stdout)
            if not isinstance(payload, dict):
                raise RuntimeError("monthly evolution output was not a JSON object")
            proposal_ids = payload.get("proposal_ids", [])
            artifacts = (
                tuple(str(value) for value in proposal_ids)
                if isinstance(proposal_ids, list)
                else ()
            )
            ledger.complete(lease, artifact_ids=artifacts)
            logger.emit(
                "job_completed",
                proposal_count=len(artifacts),
                status=payload.get("status"),
                orders_submitted=0,
                production_changes=0,
            )
            return 0
    except LockUnavailableError:
        logger.emit("tick_skipped_lock_held", level="warning")
        return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
