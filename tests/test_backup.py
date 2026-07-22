from __future__ import annotations

import os
import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from operations.backup import create_backup, prune_backups, restore_and_verify


def test_backup_restore_verifies_immutable_data_and_sqlite(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    snapshot = data_root / "accepted" / "source-id"
    snapshot.mkdir(parents=True)
    (snapshot / "manifest.json").write_text('{"dataset_id":"source-id"}')
    (snapshot / "data.parquet").write_bytes(b"immutable-test-data")
    database = tmp_path / "state" / "orders.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence VALUES ('verified')")

    archive = create_backup(
        data_root=data_root,
        sqlite_paths=(database,),
        destination_dir=tmp_path / "backups",
        include_data=True,
    )
    restored = tmp_path / "restored"
    verified = restore_and_verify(archive, restore_dir=restored)

    assert "state/orders.sqlite3" in verified
    assert "data/accepted/source-id/data.parquet" in verified
    with sqlite3.connect(restored / "state/orders.sqlite3") as connection:
        assert connection.execute("SELECT value FROM evidence").fetchone() == (
            "verified",
        )


def test_restore_rejects_archive_path_traversal(tmp_path: Path) -> None:
    source = tmp_path / "payload.txt"
    source.write_text("unsafe", encoding="utf-8")
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(source, arcname="../outside.txt")

    try:
        restore_and_verify(archive, restore_dir=tmp_path / "restored")
    except ValueError as exc:
        assert "unsafe path" in str(exc)
    else:
        raise AssertionError("unsafe backup path was accepted")


def test_backup_retention_keeps_minimum_generations(tmp_path: Path) -> None:
    now = datetime(2026, 7, 21, tzinfo=UTC)
    paths: list[Path] = []
    for index in range(6):
        path = tmp_path / f"trading-system-{index}.tar.gz"
        path.write_bytes(b"archive")
        modified = (now - timedelta(days=120 - index)).timestamp()
        os.utime(path, (modified, modified))
        paths.append(path)

    removed = prune_backups(
        tmp_path,
        retention_days=90,
        keep_last=4,
        now_utc=now,
    )

    assert set(removed) == set(paths[:2])
    assert all(path.exists() for path in paths[2:])
