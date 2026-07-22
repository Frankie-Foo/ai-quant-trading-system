from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import polars as pl

from data_plane.contracts import DataQualityCheck, DatasetSnapshot


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def persist_snapshot(
    frame: pl.DataFrame,
    *,
    root: Path,
    source: str,
    schema_version: str,
    checks: tuple[DataQualityCheck, ...],
    parent_snapshot_ids: tuple[str, ...] = (),
) -> tuple[DatasetSnapshot, Path]:
    now = datetime.now(UTC)
    temp_root = root / ".tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    temp_path = temp_root / f"{uuid4().hex}.parquet"
    frame.write_parquet(temp_path, compression="zstd", statistics=True)
    content_hash = sha256_file(temp_path)
    dataset_id = f"{source}-{now:%Y%m%dT%H%M%SZ}-{content_hash[:12]}"

    snapshot = DatasetSnapshot(
        dataset_id=dataset_id,
        source=source,
        asof_utc=now,
        content_sha256=content_hash,
        schema_version=schema_version,
        row_count=frame.height,
        parent_snapshot_ids=parent_snapshot_ids,
        checks=checks,
    )
    disposition = "accepted" if snapshot.usable else "quarantine"
    dataset_dir = root / disposition / dataset_id
    dataset_dir.mkdir(parents=True, exist_ok=False)
    final_path = dataset_dir / "data.parquet"
    temp_path.replace(final_path)
    (dataset_dir / "manifest.json").write_text(
        json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return snapshot, final_path
