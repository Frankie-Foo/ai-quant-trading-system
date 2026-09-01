"""Small local idempotency ledger for scheduled research jobs."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from db.migrations.sqlite import SQLiteMigration, apply_sqlite_migrations


class JobStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class JobRecord:
    job_name: str
    trade_date: date
    job_version: str
    status: JobStatus
    attempts: int
    started_at_utc: datetime
    finished_at_utc: datetime | None
    error_code: str | None
    artifact_ids: tuple[str, ...]


@dataclass(frozen=True)
class JobLease:
    job_name: str
    trade_date: date
    job_version: str
    run_token: str
    attempt: int


def _create_job_runs(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS job_runs (
            job_name TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            job_version TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT,
            error_code TEXT,
            artifact_ids_json TEXT NOT NULL,
            run_token TEXT,
            PRIMARY KEY (job_name, trade_date, job_version)
        )
        """
    )


def _add_job_run_token(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(job_runs)").fetchall()
    }
    if "run_token" not in columns:
        connection.execute("ALTER TABLE job_runs ADD COLUMN run_token TEXT")


JOB_LEDGER_MIGRATIONS = (
    SQLiteMigration(
        version=1,
        name="job_runs",
        signature="job_runs.v1",
        apply=_create_job_runs,
    ),
    SQLiteMigration(
        version=2,
        name="job_runs_run_token",
        signature="job_runs.run_token.v1",
        apply=_add_job_run_token,
    ),
)


class JobLedger:
    def __init__(self, path: Path, *, stale_after: timedelta = timedelta(hours=6)):
        self.path = path
        self.stale_after = stale_after
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            apply_sqlite_migrations(
                connection,
                owner="schedule.job_ledger",
                migrations=JOB_LEDGER_MIGRATIONS,
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def acquire(
        self,
        job_name: str,
        trade_date: date,
        job_version: str,
        *,
        max_attempts: int = 5,
        retry_after: timedelta = timedelta(0),
    ) -> JobLease | None:
        if not job_name.strip() or not job_version.strip():
            raise ValueError("job name and version are required")
        if max_attempts <= 0 or retry_after < timedelta(0):
            raise ValueError("retry policy is invalid")
        now = datetime.now(UTC)
        run_token = uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM job_runs WHERE job_name=? AND trade_date=? AND job_version=?",
                (job_name, trade_date.isoformat(), job_version),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO job_runs (
                        job_name, trade_date, job_version, status, attempts,
                        started_at_utc, finished_at_utc, error_code,
                        artifact_ids_json, run_token
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_name,
                        trade_date.isoformat(),
                        job_version,
                        JobStatus.RUNNING.value,
                        1,
                        now.isoformat(),
                        None,
                        None,
                        "[]",
                        run_token,
                    ),
                )
                return JobLease(
                    job_name=job_name,
                    trade_date=trade_date,
                    job_version=job_version,
                    run_token=run_token,
                    attempt=1,
                )
            status = JobStatus(str(row["status"]))
            if status is JobStatus.SUCCEEDED:
                return None
            started = datetime.fromisoformat(str(row["started_at_utc"]))
            if status is JobStatus.RUNNING and now - started < self.stale_after:
                return None
            attempts = int(row["attempts"])
            if attempts >= max_attempts:
                return None
            finished_raw = row["finished_at_utc"]
            if status is JobStatus.FAILED and finished_raw:
                finished = datetime.fromisoformat(str(finished_raw))
                if now - finished < retry_after:
                    return None
            connection.execute(
                """
                UPDATE job_runs
                SET status=?, attempts=attempts+1, started_at_utc=?, finished_at_utc=NULL,
                    error_code=NULL, artifact_ids_json='[]', run_token=?
                WHERE job_name=? AND trade_date=? AND job_version=?
                """,
                (
                    JobStatus.RUNNING.value,
                    now.isoformat(),
                    run_token,
                    job_name,
                    trade_date.isoformat(),
                    job_version,
                ),
            )
            return JobLease(
                job_name=job_name,
                trade_date=trade_date,
                job_version=job_version,
                run_token=run_token,
                attempt=attempts + 1,
            )

    def complete(
        self,
        lease: JobLease,
        *,
        artifact_ids: tuple[str, ...],
    ) -> None:
        self._finish(
            lease,
            status=JobStatus.SUCCEEDED,
            error_code=None,
            artifact_ids=artifact_ids,
        )

    def fail(
        self,
        lease: JobLease,
        *,
        error_code: str,
    ) -> None:
        self._finish(
            lease,
            status=JobStatus.FAILED,
            error_code=error_code,
            artifact_ids=(),
        )

    def _finish(
        self,
        lease: JobLease,
        *,
        status: JobStatus,
        error_code: str | None,
        artifact_ids: tuple[str, ...],
    ) -> None:
        if error_code is not None and not error_code.strip():
            raise ValueError("error code cannot be blank")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE job_runs
                SET status=?, finished_at_utc=?, error_code=?, artifact_ids_json=?
                WHERE job_name=? AND trade_date=? AND job_version=? AND status=?
                    AND run_token=?
                """,
                (
                    status.value,
                    datetime.now(UTC).isoformat(),
                    error_code,
                    json.dumps(artifact_ids),
                    lease.job_name,
                    lease.trade_date.isoformat(),
                    lease.job_version,
                    JobStatus.RUNNING.value,
                    lease.run_token,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("job lease is not current")

    def get(self, job_name: str, trade_date: date, job_version: str) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM job_runs WHERE job_name=? AND trade_date=? AND job_version=?",
                (job_name, trade_date.isoformat(), job_version),
            ).fetchone()
        if row is None:
            return None
        artifacts = json.loads(str(row["artifact_ids_json"]))
        if not isinstance(artifacts, list):
            raise ValueError("artifact ID ledger value is invalid")
        finished = row["finished_at_utc"]
        return JobRecord(
            job_name=str(row["job_name"]),
            trade_date=date.fromisoformat(str(row["trade_date"])),
            job_version=str(row["job_version"]),
            status=JobStatus(str(row["status"])),
            attempts=int(row["attempts"]),
            started_at_utc=datetime.fromisoformat(str(row["started_at_utc"])),
            finished_at_utc=(datetime.fromisoformat(str(finished)) if finished else None),
            error_code=(str(row["error_code"]) if row["error_code"] else None),
            artifact_ids=tuple(str(value) for value in artifacts),
        )
