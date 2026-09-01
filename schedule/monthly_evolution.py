"""Idempotent monthly draft-proposal scheduler."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from data_plane.calendar import build_xnys_schedule
from operations.local_env import load_project_env
from schedule.runtime import JsonEventLogger, LockUnavailableError, ProcessLock
from schedule.state import JobLedger

ROOT = Path(__file__).resolve().parents[1]
BEIJING = ZoneInfo("Asia/Shanghai")
JOB_NAME = "monthly_evolution"
JOB_VERSION = "monthly_evolution.v1"


def _run_json(arguments: list[str], *, timeout: int) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        module = arguments[arguments.index("-m") + 1]
        raise RuntimeError(f"{module} failed with exit code {completed.returncode}")
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("monthly child output was not a JSON object")
    return payload


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
    load_project_env(ROOT)
    parser = argparse.ArgumentParser()
    parser.add_argument("--asof-date", type=_parse_date, default=datetime.now(BEIJING).date())
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--state-db", type=Path, default=ROOT / "runs" / "jobs.sqlite3")
    parser.add_argument("--lock-file", type=Path, default=ROOT / "runs" / "monthly-evolution.lock")
    parser.add_argument("--llm-mode", choices=("off", "optional", "required"), default="optional")
    parser.add_argument(
        "--active-policy", type=Path, default=ROOT / "runs/strategy/active.json"
    )
    parser.add_argument(
        "--challenger-policy", type=Path, default=ROOT / "runs/strategy/challenger.json"
    )
    args = parser.parse_args(argv)
    logger = JsonEventLogger(service="monthly_evolution")
    ledger = JobLedger(args.state_db)
    lease = None
    try:
        with ProcessLock(args.lock_file):
            if not is_first_xnys_session(args.asof_date):
                logger.emit("not_first_xnys_session", asof_date=args.asof_date.isoformat())
                return 0
            lease = ledger.acquire(JOB_NAME, args.asof_date, JOB_VERSION)
            if lease is None:
                logger.emit("job_skipped", asof_date=args.asof_date.isoformat())
                return 0
            evolution = _run_json(
                [
                    "-m",
                    "scripts.run_monthly_evolution",
                    "--asof-date",
                    args.asof_date.isoformat(),
                    "--data-root",
                    str(args.data_root),
                    "--llm-mode",
                    args.llm_mode,
                ],
                timeout=1800,
            )
            proposal_ids = evolution.get("proposal_ids", [])
            artifacts = [
                str(value) for value in proposal_ids if isinstance(value, str)
            ] if isinstance(proposal_ids, list) else []
            challenger_version: str | None = None
            sandbox_decision: object = "not_run_without_memory_proposal"
            sandbox_id: object = None
            if artifacts:
                sandbox = _run_json(
                    ["-m", "scripts.run_rvol_sandbox", "--data-root", str(args.data_root)],
                    timeout=3600,
                )
                sandbox_decision = sandbox.get("decision")
                sandbox_id = sandbox.get("dataset_id")
                if sandbox_id:
                    artifacts.append(str(sandbox_id))
            if sandbox_decision == "research_champion_promoted":
                if not isinstance(sandbox_id, str) or not sandbox_id:
                    raise RuntimeError("promoted sandbox result omitted its dataset identity")
                challenger = _run_json(
                    [
                        "-m",
                        "scripts.manage_strategy_policy",
                        "build-challenger",
                        "--active",
                        str(args.active_policy),
                        "--challenger",
                        str(args.challenger_policy),
                        "--data-root",
                        str(args.data_root),
                        "--decision-dataset-id",
                        str(sandbox_id),
                    ],
                    timeout=120,
                )
                challenger_version = str(challenger.get("version", "")) or None
                if challenger_version:
                    artifacts.append(challenger_version)
            ledger.complete(lease, artifact_ids=tuple(artifacts))
            logger.emit(
                "job_completed",
                proposal_count=len(artifacts),
                status=evolution.get("status"),
                sandbox_decision=sandbox_decision,
                challenger_version=challenger_version,
                orders_submitted=0,
                production_changes=0,
            )
            return 0
    except LockUnavailableError:
        logger.emit("tick_skipped_lock_held", level="warning")
        return 0
    except Exception as exc:
        if lease is not None:
            ledger.fail(lease, error_code=type(exc).__name__)
        logger.emit(
            "job_failed",
            level="error",
            error_code=type(exc).__name__,
            orders_submitted=0,
            production_changes=0,
        )
        return 1


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
