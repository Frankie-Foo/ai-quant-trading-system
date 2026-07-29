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
