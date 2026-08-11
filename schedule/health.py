"""Read-only readiness checks for the postmarket one-shot service."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from data_plane.providers.alpaca import market_data_provider_from_env
from operations.local_env import load_project_env, project_data_root

ROOT = Path(__file__).resolve().parents[1]
CLOUD_PROVIDER_ENV = (
    "CLOUD_PLATFORM_BASE_URL",
    "CLOUD_MARKET_DATA_API_TOKEN",
)
MAX_JOB_ATTEMPTS = 5
STALE_JOB_AFTER = timedelta(hours=6)


def _check(
    name: str,
    *,
    status: str,
    detail: str,
    critical: bool,
) -> dict[str, object]:
    return {
        "name": name,
        "status": status,
        "critical": critical,
        "detail": detail,
    }


def _storage_check(data_root: Path) -> dict[str, object]:
    candidate = data_root if data_root.exists() else data_root.parent
    ready = candidate.exists() and candidate.is_dir() and os.access(candidate, os.W_OK)
    return _check(
        "storage_writable",
        status="pass" if ready else "fail",
        detail=(
            "data root or its parent is writable"
            if ready
            else "data root parent is absent or not writable"
        ),
        critical=True,
    )


def _ledger_checks(state_db: Path, *, require_success: bool) -> list[dict[str, object]]:
    if not state_db.exists():
        return [
            _check(
                "ledger_integrity",
                status="warning",
                detail="ledger does not exist before the first run",
                critical=False,
            ),
            _check(
                "prior_success",
                status="fail" if require_success else "warning",
                detail="no completed postmarket job is recorded",
                critical=require_success,
            ),
        ]
    try:
        connection = sqlite3.connect(state_db, timeout=5)
        try:
            quick = connection.execute("PRAGMA quick_check").fetchone()
            healthy = quick is not None and str(quick[0]).lower() == "ok"
            success: tuple[Any, ...] | None = None
            latest: tuple[Any, ...] | None = None
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "job_runs" in tables:
                success = connection.execute(
                    """
                    SELECT trade_date, finished_at_utc
                    FROM job_runs
                    WHERE job_name='postmarket_review' AND status='succeeded'
                    ORDER BY trade_date DESC, finished_at_utc DESC
                    LIMIT 1
                    """
                ).fetchone()
                latest = connection.execute(
                    """
                    SELECT trade_date, status, attempts, started_at_utc
                    FROM job_runs
                    WHERE job_name='postmarket_review'
                    ORDER BY trade_date DESC, started_at_utc DESC
                    LIMIT 1
                    """
                ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError):
        return [
            _check(
                "ledger_integrity",
                status="fail",
                detail="ledger cannot be opened or is not a valid SQLite database",
                critical=True,
            )
        ]
    checks = [
        _check(
            "ledger_integrity",
            status="pass" if healthy else "fail",
            detail="SQLite quick_check passed" if healthy else "SQLite quick_check failed",
            critical=True,
        )
    ]
    checks.append(
        _check(
            "prior_success",
            status="pass" if success is not None else ("fail" if require_success else "warning"),
            detail=(
                f"latest completed trade date is {success[0]}"
                if success is not None
                else "no completed postmarket job is recorded"
            ),
            critical=require_success and success is None,
        )
    )
    if latest is None:
        latest_status = "warning"
        latest_critical = False
        latest_detail = "no postmarket job state is recorded"
    else:
        status = str(latest[1])
        attempts = int(latest[2])
        started = datetime.fromisoformat(str(latest[3]))
        stale_running = status == "running" and datetime.now(UTC) - started >= STALE_JOB_AFTER
        exhausted = status == "failed" and attempts >= MAX_JOB_ATTEMPTS
        latest_critical = stale_running or exhausted
        latest_status = "fail" if latest_critical else (
            "pass" if status == "succeeded" else "warning"
        )
        if exhausted:
            latest_detail = "latest postmarket job exhausted its retry budget"
        elif stale_running:
            latest_detail = "latest postmarket job lease is stale"
        else:
            latest_detail = f"latest postmarket job status is {status} at attempt {attempts}"
    checks.append(
        _check(
            "latest_job_state",
            status=latest_status,
            detail=latest_detail,
            critical=latest_critical,
        )
    )
    return checks


def _credential_check() -> dict[str, object]:
    try:
        provider = market_data_provider_from_env()
    except RuntimeError as exc:
        return _check(
            "provider_credentials",
            status="fail",
            detail=str(exc),
            critical=True,
        )
    if provider == "alpaca_direct":
        missing = []
        if not any(
            os.getenv(name, "").strip()
            for name in (
                "ALPACA_API_KEY_ID",
                "ALPACA_PAPER_KEY_ID",
                "ALPACA_API_KEY",
            )
        ):
            missing.append("ALPACA_API_KEY_ID or ALPACA_PAPER_KEY_ID")
        if not any(
            os.getenv(name, "").strip()
            for name in (
                "ALPACA_API_SECRET_KEY",
                "ALPACA_PAPER_SECRET_KEY",
                "ALPACA_SECRET_KEY",
            )
        ):
            missing.append("ALPACA_API_SECRET_KEY or ALPACA_PAPER_SECRET_KEY")
    else:
        missing = [name for name in CLOUD_PROVIDER_ENV if not os.getenv(name, "").strip()]
    return _check(
        "provider_credentials",
        status="pass" if not missing else "fail",
        detail=(
            "required provider credential names are present"
            if not missing
            else f"missing environment variables: {', '.join(missing)}"
        ),
        critical=True,
    )


def evaluate_health(
    *,
    data_root: Path,
    state_db: Path,
    check_credentials: bool,
    require_success: bool,
) -> dict[str, object]:
    checks = [_storage_check(data_root), *_ledger_checks(state_db, require_success=require_success)]
    if check_credentials:
        checks.append(_credential_check())
    critical_failures = sum(
        1
        for check in checks
        if check["critical"] is True and check["status"] == "fail"
    )
    return {
        "ts_utc": datetime.now(UTC).isoformat(),
        "service": "postmarket",
        "status": "ready" if critical_failures == 0 else "not_ready",
        "critical_failures": critical_failures,
        "checks": checks,
    }


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    parser.add_argument("--state-db", type=Path, default=ROOT / "runs" / "jobs.sqlite3")
    parser.add_argument("--check-credentials", action="store_true")
    parser.add_argument("--require-success", action="store_true")
    args = parser.parse_args()
    result = evaluate_health(
        data_root=args.data_root,
        state_db=args.state_db,
        check_credentials=args.check_credentials,
        require_success=args.require_success,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if result["status"] == "ready" else 1)


if __name__ == "__main__":
    main()
