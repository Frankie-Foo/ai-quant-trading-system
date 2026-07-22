from __future__ import annotations

import sqlite3
from pathlib import Path

from operations.drills import run_local_safety_drills


def test_local_safety_drill_proves_no_broker_call_and_restores_state(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    snapshot = tmp_path / "data/accepted/example-id"
    snapshot.mkdir(parents=True)
    (snapshot / "manifest.json").write_text("{}", encoding="utf-8")
    (snapshot / "data.parquet").write_bytes(b"representative")
    state = tmp_path / "state"
    state.mkdir()
    with sqlite3.connect(state / "orders.sqlite3") as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES ('ok')")

    receipt, receipt_path = run_local_safety_drills(
        root=root,
        data_root=tmp_path / "data",
        state_root=state,
        backup_dir=tmp_path / "backups",
        receipt_dir=tmp_path / "receipts",
    )

    assert receipt.kill_switch_passed is True
    assert receipt.broker_calls_during_kill_switch == 0
    assert receipt.backup_restore_passed is True
    assert receipt_path.is_file()

