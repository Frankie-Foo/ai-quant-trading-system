"""Measure opportunities recovered by bypassing soft premarket gates; research only."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.storage import persist_snapshot
from research.selection_recovery import h30_recovery_features
from scripts.run_pullback_acceptance_backtest import _rth_by_date

ROOT = Path(__file__).resolve().parents[1]
COHORT_SOURCE = "research.counterfactual_candidate_cohort"
LABEL_SOURCE = "research.counterfactual_selection.labels"
SUMMARY_SOURCE = "research.counterfactual_selection.summary"


def _manifest(path: Path) -> DatasetSnapshot:
    return DatasetSnapshot.model_validate_json(
        (path.parent / "manifest.json").read_text(encoding="utf-8")
    )


def _latest_cohort(data_root: Path) -> tuple[pl.DataFrame, DatasetSnapshot]:
    matches = [
        (_manifest(path).asof_utc, path, _manifest(path))
        for path in (data_root / "accepted").glob(f"{COHORT_SOURCE}-*/data.parquet")
    ]
    if not matches:
        raise FileNotFoundError("counterfactual candidate cohort is missing")
    _, path, snapshot = max(matches, key=lambda item: item[0])
    return pl.read_parquet(path), snapshot


def _check(name: str, passed: bool, observed: object, expected: str) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=QualitySeverity.CRITICAL,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance="scripts.analyze_counterfactual_selection.v1",
    )


def _number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    return float(value)


def candidate_outcome(
    bars: pl.DataFrame,
    spy_bars: pl.DataFrame,
    *,
    session_open_utc: datetime,
) -> dict[str, float | bool] | None:
    """Return features known at 10:00 and labels strictly after 10:00."""
    features = h30_recovery_features(
        bars, spy_bars, session_open_utc=session_open_utc
    )
    if features is None:
        return None
    post = bars.filter(
        pl.col("ts_utc") >= session_open_utc + timedelta(minutes=30)
    ).sort("ts_utc")
    if post.is_empty():
        return None
    reference_px = float(post.item(0, "open"))
    return {
        **features,
        "forward_mfe_pct": _number(post.get_column("high").max(), "high") / reference_px - 1,
        "forward_mae_pct": _number(post.get_column("low").min(), "low") / reference_px - 1,
        "forward_close_pct": float(post.item(-1, "close")) / reference_px - 1,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", type=Path, default=ROOT / "runtime" / "ai-quant" / "data"
    )
    args = parser.parse_args()
    cohort, cohort_snapshot = _latest_cohort(args.data_root)
    bars_by_date, bar_parents = _rth_by_date(args.data_root)
    dates = sorted(cohort.get_column("session_date").unique().to_list())
    schedule = {
        row["trade_date"]: row
        for row in build_xnys_schedule(dates[0], dates[-1]).iter_rows(named=True)
    }
    rows: list[dict[str, object]] = []
    for row in cohort.iter_rows(named=True):
        day = row["session_date"]
        frame = bars_by_date.get(day)
        session = schedule.get(day)
        if frame is None or session is None:
            continue
        outcome = candidate_outcome(
            frame.filter(pl.col("symbol") == row["symbol"]),
            frame.filter(pl.col("symbol") == "SPY"),
            session_open_utc=session["market_open_utc"],
        )
        if outcome is None:
            continue
        rows.append(
            {
                "trade_date": day,
                "symbol": row["symbol"],
                "counterfactual_rank": row["counterfactual_rank"],
                "pass_gate": row["pass_gate"],
                "reject_reason": row["reject_reason"],
                "catalyst_tier": row["catalyst_tier"],
                **outcome,
                "clean_three_percent_opportunity": (
                    outcome["forward_mfe_pct"] >= 0.03
                    and outcome["forward_mae_pct"] > -0.02
                ),
                "production_eligible": False,
            }
        )
    labels = pl.DataFrame(rows, infer_schema_length=None)
    if labels.is_empty():
        raise FileNotFoundError("no complete counterfactual RTH labels")
    parents = (cohort_snapshot.dataset_id, *bar_parents)
    label_snapshot, _ = persist_snapshot(
        labels,
        root=args.data_root,
        source=LABEL_SOURCE,
        schema_version="counterfactual_selection_labels.v1",
        checks=(
            _check("non_empty", labels.height > 0, labels.height, ">0"),
            _check(
                "unique_candidate_day",
                labels.select("trade_date", "symbol").unique().height == labels.height,
                labels.height,
                "one row per candidate day",
            ),
            _check("research_only", True, False, "production_eligible=false"),
        ),
        parent_snapshot_ids=parents,
    )
    label_snapshot.assert_usable()
    summary = (
        labels.group_by("pass_gate")
        .agg(
            pl.len().alias("candidates"),
            pl.col("forward_mfe_pct").mean().alias("mean_forward_mfe_pct"),
            pl.col("forward_mae_pct").mean().alias("mean_forward_mae_pct"),
            pl.col("forward_close_pct").mean().alias("mean_forward_close_pct"),
            pl.col("clean_three_percent_opportunity").mean().alias("clean_opportunity_rate"),
        )
        .with_columns(
            pl.lit("diagnostic_only_new_forward_days_required").alias("status"),
            pl.lit(False).alias("production_eligible"),
        )
    )
    summary_snapshot, summary_path = persist_snapshot(
        summary,
        root=args.data_root,
        source=SUMMARY_SOURCE,
        schema_version="counterfactual_selection_summary.v1",
        checks=(_check("production_ineligible", True, False, "false"),),
        parent_snapshot_ids=(label_snapshot.dataset_id,),
    )
    summary_snapshot.assert_usable()
    recovered = labels.filter(~pl.col("pass_gate"))
    print(
        json.dumps(
            {
                "status": "complete",
                "labels": labels.height,
                "soft_rejections": recovered.height,
                "soft_rejection_clean_opportunities": recovered.filter(
                    pl.col("clean_three_percent_opportunity")
                ).height,
                "summary": summary.to_dicts(),
                "summary_path": str(summary_path),
                "production_eligible": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
