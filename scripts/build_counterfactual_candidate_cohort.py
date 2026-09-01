"""Build a research-only top-10 cohort before soft selection gates."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import polars as pl

from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.storage import persist_snapshot
from operations.local_env import project_data_root
from scripts.build_h30_candidate_cohort import latest_gate_paths

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "research.counterfactual_candidate_cohort"
MIN_MARKET_CAP_USD = 1_000_000_000.0
MAX_DAILY_CANDIDATES = 10
HARD_CATALYSTS = (
    "earnings",
    "contract_partnership",
    "regulatory_clinical",
    "merger_acquisition",
)
MEDIUM_CATALYSTS = ("other_material", "corporate_action")


def _check(name: str, passed: bool, observed: object, expected: str) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=QualitySeverity.CRITICAL,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance=SOURCE,
    )


def build_counterfactual_cohort(
    gates: Mapping[date, tuple[Path, DatasetSnapshot]],
    *,
    start: date | None = None,
    end: date | None = None,
) -> tuple[pl.DataFrame, tuple[str, ...]]:
    """Keep hard-safe names, then rank without consulting the final soft gate."""
    rows: list[pl.DataFrame] = []
    parents: list[str] = []
    for session_date, (path, snapshot) in sorted(gates.items()):
        if start is not None and session_date < start:
            continue
        if end is not None and session_date > end:
            continue
        frame = pl.read_parquet(path)
        required = {
            "market_cap",
            "current_halt",
            "luld_risk",
            "rvol",
            "premarket_price_confirmation",
        }
        if not required.issubset(frame.columns):
            continue
        eligible = frame.filter(
            (pl.col("market_cap").fill_null(0) >= MIN_MARKET_CAP_USD)
            & ~pl.col("current_halt").fill_null(True)
            & ~pl.col("luld_risk").fill_null(True)
            & pl.col("rvol").is_not_null()
            & pl.col("premarket_price_confirmation").fill_null(False)
        )
        if eligible.is_empty():
            continue
        catalyst_tier = (
            pl.when(pl.col("catalyst_categories").list.eval(pl.element().is_in(HARD_CATALYSTS)).list.any())
            .then(pl.lit(2))
            .when(pl.col("catalyst_categories").list.eval(pl.element().is_in(MEDIUM_CATALYSTS)).list.any())
            .then(pl.lit(1))
            .otherwise(pl.lit(0))
            .alias("catalyst_tier")
        )
        selected = (
            eligible.with_columns(catalyst_tier)
            .sort(
                "catalyst_tier",
                "premarket_return",
                "rvol",
                descending=[True, True, True],
                nulls_last=True,
            )
            .head(MAX_DAILY_CANDIDATES)
            .with_row_index("counterfactual_rank", offset=1)
            .select(
                "session_date",
                "symbol",
                "counterfactual_rank",
                "catalyst_tier",
                "catalyst_categories",
                "market_cap",
                "market_cap_asof_date",
                "market_cap_provenance",
                "rvol",
                "rvol_provenance",
                "premarket_return",
                "premarket_gap_return",
                "premarket_close_location",
                "premarket_above_vwap",
                "premarket_price_confirmation",
                "pass_gate",
                "reject_reason",
                "gate_asof_utc",
            )
            .with_columns(pl.lit(snapshot.dataset_id).alias("gate_snapshot_id"))
        )
        rows.append(selected)
        parents.append(snapshot.dataset_id)
    if not rows:
        raise FileNotFoundError("no hard-safe counterfactual candidates found")
    return (
        pl.concat(rows, how="diagonal_relaxed")
        .unique(("session_date", "symbol"), keep="last")
        .sort("session_date", "counterfactual_rank", "symbol"),
        tuple(parents),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    args = parser.parse_args()
    cohort, parents = build_counterfactual_cohort(
        latest_gate_paths(args.data_root), start=args.start, end=args.end
    )
    daily_max_value = cohort.group_by("session_date").len().get_column("len").max()
    minimum_market_cap = cohort.get_column("market_cap").min()
    if not isinstance(daily_max_value, (int, float)) or not isinstance(
        minimum_market_cap, (int, float)
    ):
        raise ValueError("cohort summary values must be numeric")
    daily_max = int(daily_max_value)
    snapshot, path = persist_snapshot(
        cohort,
        root=args.data_root,
        source=SOURCE,
        schema_version="counterfactual_candidate_cohort.v1",
        checks=(
            _check("non_empty", cohort.height > 0, cohort.height, ">0"),
            _check("maximum_daily_candidates", daily_max <= 10, daily_max, "<=10"),
            _check(
                "minimum_market_cap",
                float(minimum_market_cap) >= MIN_MARKET_CAP_USD,
                minimum_market_cap,
                f">={MIN_MARKET_CAP_USD}",
            ),
            _check("research_only", True, False, "production_eligible=false"),
        ),
        parent_snapshot_ids=parents,
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "sessions": cohort.get_column("session_date").n_unique(),
                "candidate_symbol_days": cohort.height,
                "soft_gate_rejections_recovered": cohort.filter(~pl.col("pass_gate")).height,
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
                "production_eligible": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
