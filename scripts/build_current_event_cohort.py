"""Build the three-year catalyst cohort at the current 20:00 Beijing lock."""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.storage import persist_snapshot
from kernel.catalysts import prepare_catalysts
from operations.local_env import project_data_root
from research.event_cohort import build_event_cohort
from research.history import premarket_decision_asof_utc

ROOT = Path(__file__).resolve().parents[1]
NEWS_SOURCE = "massive.news.history"
SOURCE = "research.current_event_cohort"


def _snapshot(path: Path) -> DatasetSnapshot:
    return DatasetSnapshot.model_validate_json(
        (path.parent / "manifest.json").read_text(encoding="utf-8")
    )


def _load_news(data_root: Path) -> tuple[pl.DataFrame, tuple[str, ...]]:
    paths = list((data_root / "accepted").glob(f"{NEWS_SOURCE}-*/data.parquet"))
    if not paths:
        raise FileNotFoundError("historical Massive news partitions are missing")
    frame = (
        pl.scan_parquet([str(path) for path in paths])
        .collect()
        .unique(("source", "source_event_id"), keep="last")
        .sort("published_utc", "source", "source_event_id")
    )
    return frame, tuple(_snapshot(path).dataset_id for path in paths)


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
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    args = parser.parse_args()
    if args.end < args.start:
        raise ValueError("end must not precede start")

    schedule = build_xnys_schedule(args.start - timedelta(days=10), args.end)
    targets = schedule.filter(
        (pl.col("trade_date") >= args.start) & (pl.col("trade_date") <= args.end)
    ).get_column("trade_date").to_list()
    news, parents = _load_news(args.data_root)
    prepared = prepare_catalysts(
        news,
        asof_utc=premarket_decision_asof_utc(args.end),
    )
    cohort = build_event_cohort(
        prepared,
        schedule=schedule,
        target_dates=targets,
    )
    if cohort.is_empty():
        raise ValueError("current-lock catalyst cohort is empty")
    future = cohort.filter(
        pl.col("latest_event_utc") > pl.col("decision_asof_utc")
    ).height
    duplicates = cohort.height - cohort.select(
        pl.struct("session_date", "symbol").n_unique()
    ).item()
    snapshot, path = persist_snapshot(
        cohort,
        root=args.data_root,
        source=SOURCE,
        schema_version="current_event_cohort.v1",
        checks=(
            _check("non_empty", cohort.height > 0, cohort.height, ">0"),
            _check("unique_symbol_session", duplicates == 0, duplicates, "0"),
            _check("no_future_event", future == 0, future, "0"),
        ),
        parent_snapshot_ids=parents,
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "sessions": len(targets),
                "sessions_with_candidates": cohort["session_date"].n_unique(),
                "symbol_days": cohort.height,
                "unique_symbols": cohort["symbol"].n_unique(),
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
