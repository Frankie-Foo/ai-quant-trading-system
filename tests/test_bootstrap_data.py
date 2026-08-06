from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from operations.bootstrap_data import BootstrapImporter


def _archive(path: Path, *, traversal: bool = False) -> Path:
    payload = b"accepted snapshot"
    relative = (
        "../outside.txt"
        if traversal
        else "accepted/massive.news.history-demo/data.parquet"
    )
    manifest = {
        "schema_version": "desktop_bootstrap.v1",
        "files": [
            {
                "path": relative,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bootstrap-manifest.json", json.dumps(manifest))
        archive.writestr(relative, payload)
    return path


def test_bootstrap_imports_missing_snapshots_and_preserves_existing_data(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path / "bootstrap.zip")
    data_root = tmp_path / "data"
    runs_root = tmp_path / "runs"
    importer = BootstrapImporter(
        archive_path=archive,
        data_root=data_root,
        runs_root=runs_root,
    )

    first = importer.import_if_needed()
    target = data_root / "accepted" / "massive.news.history-demo" / "data.parquet"
    target.write_bytes(b"newer local snapshot")
    second = importer.import_if_needed(force=True)

    assert first["status"] == "complete"
    assert first["imported_datasets"] == 1
    assert target.read_bytes() == b"newer local snapshot"
    assert second["skipped_existing_datasets"] == 1
    assert (runs_root / "bootstrap-import.json").is_file()


def test_bootstrap_rejects_archive_path_traversal(tmp_path: Path) -> None:
    importer = BootstrapImporter(
        archive_path=_archive(tmp_path / "unsafe.zip", traversal=True),
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
    )

    with pytest.raises(ValueError, match="unsafe bootstrap path"):
        importer.import_if_needed()

    assert not (tmp_path / "outside.txt").exists()


def test_bootstrap_skips_and_repairs_duplicate_grouped_daily_dates(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    accepted = data_root / "accepted"
    existing = accepted / "massive.grouped_daily-existing"
    existing.mkdir(parents=True)
    frame = pl.DataFrame(
        {
            "symbol": ["AAPL"],
            "trade_date": [date(2026, 7, 30)],
        }
    )
    frame.write_parquet(existing / "data.parquet")
    archive_path = tmp_path / "daily.zip"
    payload_path = tmp_path / "data.parquet"
    frame.write_parquet(payload_path)
    payload = payload_path.read_bytes()
    relative = "accepted/massive.grouped_daily-bootstrap/data.parquet"
    manifest = {
        "schema_version": "desktop_bootstrap.v1",
        "files": [{
            "path": relative,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }],
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("bootstrap-manifest.json", json.dumps(manifest))
        archive.writestr(relative, payload)

    result = BootstrapImporter(
        archive_path=archive_path,
        data_root=data_root,
        runs_root=tmp_path / "runs",
    ).import_if_needed()

    assert result["skipped_semantic_duplicates"] == 1
    assert len(list(accepted.glob("massive.grouped_daily-*"))) == 1
