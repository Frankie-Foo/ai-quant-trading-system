"""Load immutable point-in-time premarket pools without coupling them to catalysts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DatasetSnapshot
from data_plane.snapshot_queries import load_snapshot_by_id

PremarketPoolName = Literal["catalyst", "factor"]


@dataclass(frozen=True)
class PremarketPool:
    frame: pl.DataFrame
    snapshot: DatasetSnapshot
    source: str
    target_date: date


def _manifest(path: Path) -> DatasetSnapshot:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"manifest is not an object: {path}")
    return DatasetSnapshot.model_validate(value)


def _previous_session(target_date: date) -> date:
    schedule = build_xnys_schedule(target_date - timedelta(days=10), target_date)
    dates = schedule.get_column("trade_date").to_list()
    previous = [value for value in dates if value < target_date]
    if not previous:
        raise ValueError(f"previous XNYS session unavailable for {target_date}")
    return cast(date, previous[-1])


def _matches_target(
    frame: pl.DataFrame,
    *,
    pool: PremarketPoolName,
    target_date: date,
) -> bool:
    if pool == "catalyst":
        return (
            "session_date" in frame.columns
            and frame.get_column("session_date").unique().to_list() == [target_date]
        )
    if "session_date" in frame.columns:
        return frame.get_column("session_date").unique().to_list() == [target_date]
    return (
        "asof_date" in frame.columns
        and frame.get_column("asof_date").unique().to_list()
        == [_previous_session(target_date)]
    )


def load_premarket_pool(
    data_root: Path,
    target_date: date,
    *,
    pool: PremarketPoolName,
    snapshot_id: str | None = None,
    decision_cutoff: datetime | None = None,
) -> PremarketPool:
    """Return the latest accepted catalyst or independent daily-factor pool."""

    if snapshot_id is not None:
        if pool != "catalyst":
            raise ValueError("explicit event snapshot requires catalyst pool")
        source = "kernel.catalysts.wave_candidates"
        frame, snapshot = load_snapshot_by_id(
            data_root, snapshot_id, source=source, available_by=datetime.now(UTC),
        )
        context = [check for check in snapshot.checks if check.name == "wave_context"]
        if len(context) != 1 or context[0].observed != target_date.isoformat():
            raise ValueError("wave context date mismatch")
        cutoff = datetime.fromisoformat(context[0].expected)
        if (cutoff.tzinfo is None or cutoff.utcoffset() is None
                or cutoff > snapshot.asof_utc
                or (decision_cutoff is not None and cutoff != decision_cutoff)):
            raise ValueError("wave context cutoff mismatch")
        if not frame.is_empty() and not _matches_target(
            frame, pool="catalyst", target_date=target_date,
        ):
            raise ValueError("wave context rows mismatch")
        if frame["symbol"].n_unique() != frame.height:
            raise ValueError("wave pool contains duplicate symbols")
        return PremarketPool(frame.sort("symbol"), snapshot, source, target_date)
    if pool == "catalyst":
        source = "kernel.catalysts.overnight_candidates"
    elif pool == "factor":
        source = "kernel.universe.daily_precheck"
    else:
        raise ValueError("pool must be 'catalyst' or 'factor'")
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    pattern = f"{source}-*/data.parquet"
    for path in (data_root / "accepted").glob(pattern):
        frame = pl.read_parquet(path)
        if not _matches_target(frame, pool=pool, target_date=target_date):
            continue
        snapshot = _manifest(path.parent / "manifest.json")
        snapshot.assert_usable()
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        raise FileNotFoundError(f"no accepted {pool} pool for {target_date}")

    _, path, snapshot = max(matches)
    frame = pl.read_parquet(path)
    if pool == "factor":
        required = {"symbol", "precheck_pass", "reject_reason"}
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(
                f"daily factor pool missing required columns: {sorted(missing)}"
            )
        frame = frame.filter(pl.col("precheck_pass")).sort("symbol")
    else:
        if "symbol" not in frame.columns:
            raise ValueError("catalyst pool missing symbol")
        frame = frame.sort("symbol")
    if frame.get_column("symbol").n_unique() != frame.height:
        raise ValueError(f"{pool} pool contains duplicate symbols")
    return PremarketPool(
        frame=frame,
        snapshot=snapshot,
        source=source,
        target_date=target_date,
    )
