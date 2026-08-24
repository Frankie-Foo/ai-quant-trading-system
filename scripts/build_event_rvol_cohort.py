"""Build the top-10 event cohort after exact 20-session premarket RVOL."""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.storage import persist_snapshot
from operations.local_env import project_data_root
from research.event_rvol import MAX_DAILY_CANDIDATES, MIN_RVOL, build_event_rvol_cohort

ROOT = Path(__file__).resolve().parents[1]
GAP_SOURCE = "research.current_event_premarket_gap"
BAR_SOURCE = "alpaca.sip.current_event_premarket_rvol_history"
SOURCE = "research.current_event_rvol_cohort"


def _snapshot(path: Path) -> DatasetSnapshot:
    return DatasetSnapshot.model_validate_json(
        (path.parent / "manifest.json").read_text(encoding="utf-8")
    )


def _latest(data_root: Path, source: str) -> tuple[Path, DatasetSnapshot]:
    matches = [
        (_snapshot(path).asof_utc, path, _snapshot(path))
        for path in (data_root / "accepted").glob(f"{source}-*/data.parquet")
    ]
    if not matches:
        raise FileNotFoundError(f"dataset is missing: {source}")
    _, path, snapshot = max(matches)
    return path, snapshot


def _check(name: str, passed: bool, observed: object, expected: str) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=QualitySeverity.CRITICAL,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance=SOURCE,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    args = parser.parse_args()
    gap_path, gap_snapshot = _latest(args.data_root, GAP_SOURCE)
    gap = pl.read_parquet(gap_path)
    bar_paths = list((args.data_root / "accepted").glob(f"{BAR_SOURCE}-*/data.parquet"))
    if not bar_paths:
        raise FileNotFoundError("premarket RVOL history is missing")
    bar_snapshots = tuple(_snapshot(path) for path in bar_paths)
    bars = pl.scan_parquet([str(path) for path in bar_paths]).collect().unique(
        ("symbol", "ts_utc"), keep="last"
    )
    targets = gap.get_column("session_date").sort().unique().to_list()
    schedule = build_xnys_schedule(targets[0] - timedelta(days=60), targets[-1])
    result = build_event_rvol_cohort(gap, bars, schedule=schedule)
    if result.is_empty():
        raise ValueError("RVOL cohort is empty")
    daily_max = result.group_by("session_date").len().get_column("len").max()
    invalid = result.filter(pl.col("premarket_rvol") < MIN_RVOL).height
    snapshot, path = persist_snapshot(
        result,
        root=args.data_root,
        source=SOURCE,
        schema_version="current_event_rvol_cohort.v1",
        checks=(
            _check("non_empty", result.height > 0, result.height, ">0"),
            _check("minimum_rvol", invalid == 0, invalid, "0"),
            _check(
                "maximum_daily_candidates",
                isinstance(daily_max, (int, float)) and daily_max <= MAX_DAILY_CANDIDATES,
                daily_max,
                f"<={MAX_DAILY_CANDIDATES}",
            ),
        ),
        parent_snapshot_ids=(
            gap_snapshot.dataset_id,
            *(item.dataset_id for item in bar_snapshots),
        ),
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "symbol_days": result.height,
                "sessions": result["session_date"].n_unique(),
                "symbols": result["symbol"].n_unique(),
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
