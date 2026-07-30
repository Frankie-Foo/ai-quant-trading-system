"""Read-only queries over immutable accepted dataset snapshots."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import polars as pl

from data_plane.contracts import DatasetSnapshot


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
