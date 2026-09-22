"""Read-only queries over immutable accepted dataset snapshots."""

from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import date, datetime
from pathlib import Path

import polars as pl

from data_plane.contracts import DatasetSnapshot


def load_snapshot_by_id(
    data_root: Path, dataset_id: str, *, source: str,
    available_by: datetime | None = None,
    required_parents: tuple[str, ...] = (),
) -> tuple[pl.DataFrame, DatasetSnapshot]:
    """Read exactly one accepted, hash-verified snapshot; never fall back to latest."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", dataset_id):
        raise ValueError("invalid snapshot identity")
    accepted = (data_root / "accepted").resolve()
    directory = (accepted / dataset_id).resolve()
    if directory.parent != accepted:
        raise ValueError("snapshot path leaves accepted storage")
    snapshot = _manifest(directory / "manifest.json")
    if snapshot.dataset_id != dataset_id or snapshot.source != source:
        raise ValueError("snapshot identity/source mismatch")
    if available_by is not None:
        if available_by.tzinfo is None or available_by.utcoffset() is None:
            raise ValueError("available_by must be timezone-aware")
        if snapshot.asof_utc > available_by:
            raise ValueError("snapshot was not available at decision time")
    if not set(required_parents).issubset(snapshot.parent_snapshot_ids):
        raise ValueError("snapshot parent mismatch")
    content = (directory / "data.parquet").read_bytes()
    if hashlib.sha256(content).hexdigest() != snapshot.content_sha256:
        raise ValueError("snapshot content hash mismatch")
    frame = pl.read_parquet(io.BytesIO(content))
    if frame.height != snapshot.row_count:
        raise ValueError("snapshot row count mismatch")
    return frame, snapshot


def _manifest(path: Path) -> DatasetSnapshot:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"manifest is not an object: {path}")
    return DatasetSnapshot.model_validate(value).assert_usable()


def load_latest_session_snapshot(
    data_root: Path,
    *,
    source: str,
    session_date: date,
    session_column: str = "session_date",
) -> tuple[pl.DataFrame, DatasetSnapshot]:
    """Load the newest usable snapshot containing exactly one requested session."""

    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(f"{source}-*/data.parquet"):
        try:
            session_frame = pl.read_parquet(path, columns=[session_column])
        except pl.exceptions.ColumnNotFoundError:
            continue
        dates = session_frame.get_column(session_column).unique().to_list()
        if dates != [session_date]:
            continue
        snapshot = _manifest(path.parent / "manifest.json")
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        raise FileNotFoundError(f"no {source} snapshot for {session_date}")
    _, path, snapshot = max(matches)
    return pl.read_parquet(path), snapshot
