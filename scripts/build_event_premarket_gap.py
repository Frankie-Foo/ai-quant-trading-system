"""Build the causal +4% premarket-gap cohort from current-day SIP bars."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import polars as pl

from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.storage import persist_snapshot
from operations.local_env import project_data_root

ROOT = Path(__file__).resolve().parents[1]
PREFILTER_SOURCE = "research.current_event_daily_prefilter"
BAR_SOURCE = "alpaca.sip.current_event_premarket"
SOURCE = "research.current_event_premarket_gap"
MIN_GAP_RETURN = 0.04


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


def build_gap_cohort(prefilter: pl.DataFrame, bars: pl.DataFrame) -> pl.DataFrame:
    features = (
        bars.with_columns(
            pl.col("ts_utc")
            .dt.convert_time_zone("America/New_York")
            .dt.date()
            .alias("session_date"),
            (pl.col("vwap") * pl.col("volume")).alias("dollar_value"),
        )
        .sort("symbol", "ts_utc")
        .group_by("session_date", "symbol")
        .agg(
            pl.col("close").last().alias("premarket_close"),
            pl.col("high").max().alias("premarket_high"),
            pl.col("low").min().alias("premarket_low"),
            pl.col("volume").sum().alias("premarket_volume"),
            (pl.col("dollar_value").sum() / pl.col("volume").sum()).alias(
                "premarket_vwap"
            ),
            pl.col("ts_utc").max().alias("latest_bar_utc"),
        )
    )
    return (
        prefilter.join(features, on=("session_date", "symbol"), how="inner", validate="1:1")
        .with_columns(
            (pl.col("premarket_close") / pl.col("prior_close") - 1).alias(
                "premarket_gap_return"
            )
        )
        .filter(pl.col("premarket_gap_return") >= MIN_GAP_RETURN)
        .sort("session_date", "premarket_gap_return", descending=[False, True])
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    args = parser.parse_args()
    prefilter_path, prefilter_snapshot = _latest(args.data_root, PREFILTER_SOURCE)
    bar_paths = list((args.data_root / "accepted").glob(f"{BAR_SOURCE}-*/data.parquet"))
    if not bar_paths:
        raise FileNotFoundError("current event premarket bars are missing")
    bar_snapshots = tuple(_snapshot(path) for path in bar_paths)
    bars = pl.scan_parquet([str(path) for path in bar_paths]).collect().unique(
        ("symbol", "ts_utc"), keep="last"
    )
    result = build_gap_cohort(pl.read_parquet(prefilter_path), bars)
    duplicates = result.height - result.select(
        pl.struct("session_date", "symbol").n_unique()
    ).item()
    invalid = result.filter(pl.col("premarket_gap_return") < MIN_GAP_RETURN).height
    snapshot, path = persist_snapshot(
        result,
        root=args.data_root,
        source=SOURCE,
        schema_version="current_event_premarket_gap.v1",
        checks=(
            _check("non_empty", result.height > 0, result.height, ">0"),
            _check("unique_symbol_session", duplicates == 0, duplicates, "0"),
            _check("minimum_gap", invalid == 0, invalid, "0"),
        ),
        parent_snapshot_ids=(
            prefilter_snapshot.dataset_id,
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
