from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.storage import persist_snapshot
from kernel.config import load_config
from research.sandbox import evaluate_rvol_challengers

ROOT = Path(__file__).resolve().parents[1]
LABEL_SOURCE = "research.history.net_labels"
METRIC_SOURCE = "research.sandbox.rvol_threshold_metrics"
DECISION_SOURCE = "research.sandbox.rvol_champion_decision"


def _manifest(path: Path) -> DatasetSnapshot:
    return DatasetSnapshot.model_validate_json(
        (path.parent / "manifest.json").read_text(encoding="utf-8")
    )


def _latest_labels(data_root: Path) -> tuple[pl.DataFrame, DatasetSnapshot]:
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(f"{LABEL_SOURCE}-*/data.parquet"):
        snapshot = _manifest(path)
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        raise FileNotFoundError("cost-complete historical labels are missing")
    _, path, snapshot = max(matches, key=lambda item: item[0])
    return pl.read_parquet(path), snapshot


def _check(name: str, passed: bool, observed: Any, expected: str) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=QualitySeverity.CRITICAL,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance="research.sandbox.evaluate_rvol_challengers.v1",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    labels, label_snapshot = _latest_labels(args.data_root)
    cfg = load_config(ROOT / "config.yaml")
    metrics, decision = evaluate_rvol_challengers(
        labels,
        baseline=cfg.universe.min_rvol,
        max_concurrent=cfg.max_concurrent,
    )
    metric_snapshot, _ = persist_snapshot(
        metrics,
        root=args.data_root,
        source=METRIC_SOURCE,
        schema_version="rvol_threshold_oos_metrics.v1",
        checks=(
            _check(
                "exact_folds",
                metrics["fold"].n_unique() == 5,
                metrics["fold"].n_unique(),
                "5",
            ),
            _check(
                "exact_configurations",
                metrics["threshold"].n_unique() == 4,
                metrics["threshold"].n_unique(),
                "4 including baseline",
            ),
        ),
        parent_snapshot_ids=(label_snapshot.dataset_id,),
    )
    metric_snapshot.assert_usable()
    decision_frame = pl.DataFrame([asdict(decision)])
    decision_snapshot, decision_path = persist_snapshot(
        decision_frame,
        root=args.data_root,
        source=DECISION_SOURCE,
        schema_version="rvol_research_champion_decision.v1",
        checks=(
            _check(
                "not_production_eligible",
                not decision.production_eligible,
                decision.production_eligible,
                "false",
            ),
            _check(
                "attempt_budget",
                decision.attempted_configurations == 3,
                decision.attempted_configurations,
                "3",
            ),
        ),
        parent_snapshot_ids=(metric_snapshot.dataset_id,),
    )
    decision_snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "decision": decision.status,
                "research_champion_rvol": decision.selected,
                "production_eligible": decision.production_eligible,
                "dataset_id": decision_snapshot.dataset_id,
                "path": str(decision_path),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
