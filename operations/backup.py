"""Verified backups for immutable snapshots and live SQLite state."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _online_sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as source_connection, closing(
        sqlite3.connect(destination)
    ) as destination_connection:
        source_connection.backup(destination_connection)
        result = destination_connection.execute("PRAGMA quick_check").fetchone()
        if result is None or str(result[0]).lower() != "ok":
            raise RuntimeError(f"SQLite backup quick_check failed: {source.name}")


def create_backup(
    *,
    data_root: Path,
    sqlite_paths: tuple[Path, ...],
    destination_dir: Path,
    include_data: bool,
) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    archive = destination_dir / f"trading-system-{timestamp}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="trading-backup-") as temporary:
        staging = Path(temporary)
        state = staging / "state"
        state.mkdir()
        for source in sqlite_paths:
            if source.exists():
                _online_sqlite_backup(source, state / source.name)
        if include_data:
            accepted = data_root / "accepted"
            if not accepted.is_dir():
                raise FileNotFoundError("accepted immutable data directory is missing")
            shutil.copytree(accepted, staging / "data" / "accepted")

        files = sorted(
            path for path in staging.rglob("*") if path.is_file()
        )
        manifest = {
            "created_at_utc": datetime.now(UTC).isoformat(),
            "include_data": include_data,
            "files": {
                path.relative_to(staging).as_posix(): _sha256(path) for path in files
            },
        }
        (staging / "backup_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        with tarfile.open(archive, "w:gz") as handle:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    handle.add(path, arcname=path.relative_to(staging).as_posix())
    return archive


def restore_and_verify(archive: Path, *, restore_dir: Path) -> dict[str, str]:
    restore_dir.mkdir(parents=True, exist_ok=True)
    if any(restore_dir.iterdir()):
        raise ValueError("restore directory must be empty")
    with tarfile.open(archive, "r:gz") as handle:
        for member in handle.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("backup archive contains an unsafe path")
        handle.extractall(restore_dir, filter="data")
    manifest_path = restore_dir / "backup_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, dict):
        raise ValueError("backup manifest file table is invalid")
    verified: dict[str, str] = {}
    for relative, expected in files.items():
        relative_path = PurePosixPath(str(relative))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("backup manifest contains an unsafe path")
        restored_path = restore_dir.joinpath(*relative_path.parts)
        if not restored_path.is_file():
            raise ValueError(f"backup file is missing: {relative}")
        actual = _sha256(restored_path)
        if actual != expected:
            raise ValueError(f"backup hash mismatch: {relative}")
        verified[str(relative)] = actual
        if restored_path.suffix in {".sqlite", ".sqlite3", ".db"}:
            with closing(sqlite3.connect(restored_path)) as connection:
                result = connection.execute("PRAGMA quick_check").fetchone()
            if result is None or str(result[0]).lower() != "ok":
                raise ValueError(f"restored SQLite quick_check failed: {relative}")
    return verified


def prune_backups(
    destination_dir: Path,
    *,
    retention_days: int,
    keep_last: int,
    now_utc: datetime | None = None,
) -> tuple[Path, ...]:
    """Remove only this program's expired archives, retaining a minimum generation set."""
    if retention_days < 1 or keep_last < 1:
        raise ValueError("retention_days and keep_last must be positive")
    now = now_utc or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("now_utc must be timezone-aware UTC")
    archives = sorted(
        destination_dir.glob("trading-system-*.tar.gz"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    protected = set(archives[:keep_last])
    cutoff = now - timedelta(days=retention_days)
    removed: list[Path] = []
    for archive in archives:
        modified = datetime.fromtimestamp(archive.stat().st_mtime, UTC)
        if archive not in protected and modified < cutoff:
            archive.unlink()
            removed.append(archive)
    return tuple(removed)
