"""Build the causal daily-liquidity prefilter for the current event cohort."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import polars as pl

from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.storage import persist_snapshot
from operations.local_env import project_data_root
from research.event_prefilter import MIN_ADV20_USD, MIN_PRICE, daily_event_prefilter

ROOT = Path(__file__).resolve().parents[1]
COHORT_SOURCE = "research.current_event_cohort"
DAILY_SOURCE = "alpaca.sip.daily_event_symbols"
SOURCE = "research.current_event_daily_prefilter"


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
    data_root = args.data_root
    cohort_path, cohort_snapshot = _latest(data_root, COHORT_SOURCE)
    daily_paths = list(
        (data_root / "accepted").glob(f"{DAILY_SOURCE}-*/data.parquet")
    )
    if not daily_paths:
        raise FileNotFoundError("event daily bar chunks are missing")
    daily_snapshots = tuple(_snapshot(path) for path in daily_paths)
    daily = (
        pl.scan_parquet([str(path) for path in daily_paths])
        .collect()
        .unique(("symbol", "ts_utc"), keep="last")
    )
    result = daily_event_prefilter(pl.read_parquet(cohort_path), daily)
    duplicates = result.height - result.select(
        pl.struct("session_date", "symbol").n_unique()
    ).item()
    invalid = result.filter(
        (pl.col("catalyst_tier") < 1)
        | (pl.col("prior_close") < MIN_PRICE)
        | (pl.col("prior_adv20_usd") < MIN_ADV20_USD)
    ).height
    snapshot, path = persist_snapshot(
        result,
        root=data_root,
        source=SOURCE,
        schema_version="current_event_daily_prefilter.v1",
        checks=(
            _check("non_empty", result.height > 0, result.height, ">0"),
            _check("unique_symbol_session", duplicates == 0, duplicates, "0"),
            _check("daily_hard_gates", invalid == 0, invalid, "0"),
        ),
        parent_snapshot_ids=(
            cohort_snapshot.dataset_id,
            *(item.dataset_id for item in daily_snapshots),
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
