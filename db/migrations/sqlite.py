"""Small, explicit SQLite migration runner for domain-owned ledgers."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

MigrationOperation = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class SQLiteMigration:
    version: int
    name: str
    signature: str
    apply: MigrationOperation

    def __post_init__(self) -> None:
        if self.version <= 0:
            raise ValueError("migration version must be positive")
        if not self.name.strip() or not self.signature.strip():
            raise ValueError("migration name and signature are required")


def apply_sqlite_migrations(
    connection: sqlite3.Connection,
    *,
    owner: str,
    migrations: Iterable[SQLiteMigration],
) -> None:
    """Apply ordered migrations and record an immutable checksum per owner/version."""

    normalized_owner = owner.strip()
    if not normalized_owner:
        raise ValueError("migration owner is required")
    ordered = tuple(sorted(migrations, key=lambda item: item.version))
    versions = tuple(item.version for item in ordered)
    if len(set(versions)) != len(versions):
        raise ValueError("migration versions must be unique")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            owner TEXT NOT NULL,
            version INTEGER NOT NULL,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at_utc TEXT NOT NULL,
            PRIMARY KEY (owner, version)
        )
        """
    )
    connection.commit()
    for migration in ordered:
        checksum = hashlib.sha256(
            f"{normalized_owner}:{migration.version}:{migration.name}:"
            f"{migration.signature}".encode()
        ).hexdigest()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT checksum FROM schema_migrations WHERE owner=? AND version=?",
                (normalized_owner, migration.version),
            ).fetchone()
            if row is not None:
                if str(row[0]) != checksum:
                    raise RuntimeError(
                        "migration checksum mismatch: "
                        f"{normalized_owner}:{migration.version}"
                    )
                connection.commit()
                continue
            migration.apply(connection)
            connection.execute(
                """
                INSERT INTO schema_migrations (
                    owner, version, name, checksum, applied_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    normalized_owner,
                    migration.version,
                    migration.name,
                    checksum,
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
