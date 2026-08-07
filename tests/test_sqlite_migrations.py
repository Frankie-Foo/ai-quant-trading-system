import sqlite3
from pathlib import Path

import pytest

from db.migrations.sqlite import SQLiteMigration, apply_sqlite_migrations
from execution.ledger import OrderLedger
from schedule.state import JobLedger


def _create_sample_table(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")


def _fail_after_creating_sample(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE failed_sample (value TEXT NOT NULL)")
    raise RuntimeError("migration failure")


def test_sqlite_migrations_are_idempotent_and_record_checksums() -> None:
    with sqlite3.connect(":memory:") as connection:
        migrations = (
            SQLiteMigration(
                version=1,
                name="sample",
                signature="sample.v1",
                apply=_create_sample_table,
            ),
        )
        apply_sqlite_migrations(connection, owner="test", migrations=migrations)
        apply_sqlite_migrations(connection, owner="test", migrations=migrations)

        rows = connection.execute(
            "SELECT owner, version, name FROM schema_migrations"
        ).fetchall()
        assert rows == [("test", 1, "sample")]

        with pytest.raises(RuntimeError, match="checksum mismatch"):
            apply_sqlite_migrations(
                connection,
                owner="test",
                migrations=(
                    SQLiteMigration(
                        version=1,
                        name="sample",
                        signature="sample.changed",
                        apply=lambda _db: None,
                    ),
                ),
            )


def test_failed_migration_rolls_back_schema_and_version_record() -> None:
    with sqlite3.connect(":memory:") as connection:
        with pytest.raises(RuntimeError, match="migration failure"):
            apply_sqlite_migrations(
                connection,
                owner="test",
                migrations=(
                    SQLiteMigration(
                        version=1,
                        name="failed_sample",
                        signature="failed_sample.v1",
                        apply=_fail_after_creating_sample,
                    ),
                ),
            )
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='failed_sample'"
        ).fetchone()
        version = connection.execute(
            "SELECT version FROM schema_migrations WHERE owner='test'"
        ).fetchone()
    assert table is None
    assert version is None


def test_job_ledger_upgrades_legacy_schema(tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE job_runs (
                job_name TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                job_version TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                started_at_utc TEXT NOT NULL,
                finished_at_utc TEXT,
                error_code TEXT,
                artifact_ids_json TEXT NOT NULL,
                PRIMARY KEY (job_name, trade_date, job_version)
            )
            """
        )

    JobLedger(path)

    with sqlite3.connect(path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(job_runs)").fetchall()
        }
        versions = connection.execute(
            "SELECT version FROM schema_migrations "
            "WHERE owner='schedule.job_ledger' ORDER BY version"
        ).fetchall()
    assert "run_token" in columns
    assert versions == [(1,), (2,)]


def test_order_ledger_records_its_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "orders.sqlite3"
    OrderLedger(path)

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT version, name FROM schema_migrations "
            "WHERE owner='execution.order_ledger'"
        ).fetchone()
    assert row == (1, "orders_and_trade_plans")
