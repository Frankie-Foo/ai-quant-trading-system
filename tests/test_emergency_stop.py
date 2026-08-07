from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from operations.emergency_stop import EmergencyStopStore

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def test_emergency_stop_is_durable_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "emergency.sqlite3"
    store = EmergencyStopStore(path)

    first = store.activate(at_utc=NOW, reason="desktop_global_stop")
    replay = store.activate(at_utc=NOW, reason="desktop_global_stop")
    restarted = EmergencyStopStore(path)

    assert first.active is True
    assert replay == first
    assert restarted.read() == first


def test_emergency_stop_schema_is_versioned(tmp_path: Path) -> None:
    store = EmergencyStopStore(tmp_path / "emergency.sqlite3")

    with store._connect() as connection:
        row = connection.execute(
            """
            SELECT owner, version, name
            FROM schema_migrations
            WHERE owner = 'operations.emergency_stop'
            """
        ).fetchone()

    assert row is not None
    assert tuple(row) == ("operations.emergency_stop", 1, "emergency_stop")
