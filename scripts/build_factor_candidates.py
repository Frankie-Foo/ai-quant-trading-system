"""Build an immutable pure-factor candidate snapshot with no catalyst dependency."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from data_plane.candidate_pools import load_premarket_pool
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.storage import persist_snapshot
from kernel.config import load_config
from kernel.factor_selection import FactorSelectionPolicy, select_factor_candidates

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "kernel.selection.factor_candidates_shadow"
RVOL_SOURCE = "kernel.premarket.factor_rvol_candidates"


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _manifest(path: Path) -> DatasetSnapshot:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"manifest is not an object: {path}")
    return DatasetSnapshot.model_validate(value).assert_usable()


def _load_factor_rvol(
    data_root: Path, trade_date: date
) -> tuple[pl.DataFrame, DatasetSnapshot]:
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(f"{RVOL_SOURCE}-*/data.parquet"):
        frame = pl.read_parquet(path, columns=["session_date"])
        if frame.get_column("session_date").unique().to_list() != [trade_date]:
            continue
        snapshot = _manifest(path.parent / "manifest.json")
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        raise FileNotFoundError(
            f"no factor RVOL snapshot for {trade_date}; run "
            "scripts.build_premarket_rvol --pool factor first"
        )
    _, path, snapshot = max(matches)
    return pl.read_parquet(path), snapshot


def _check(
    name: str,
    passed: bool,
    observed: object,
    expected: str,
    provenance: str,
) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=QualitySeverity.CRITICAL,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance=provenance,
    )


def build_factor_candidate_snapshot(
    daily_universe: pl.DataFrame,
    premarket_features: pl.DataFrame,
    *,
    daily_snapshot: DatasetSnapshot,
    rvol_snapshot: DatasetSnapshot,
    data_root: Path,
    trade_date: date,
    asof_utc: datetime,
    policy: FactorSelectionPolicy,
) -> tuple[pl.DataFrame, DatasetSnapshot, Path]:
    """Persist the pure-factor module output behind one auditable interface."""

    frame = select_factor_candidates(
        daily_universe,
        premarket_features,
        trade_date=trade_date,
        asof_utc=asof_utc,
        policy=policy,
    )
    duplicate_count = (
        0
        if frame.is_empty()
        else frame.height - frame.get_column("symbol").n_unique()
    )
    selected = frame.filter(pl.col("factor_pass"))
    expected_ranks = list(range(1, selected.height + 1))
    actual_ranks = (
        selected.sort("factor_rank").get_column("factor_rank").to_list()
    )
    provenance = f"{SOURCE}@{asof_utc.isoformat()}"
    checks = (
        _check(
            "unique_symbol",
            duplicate_count == 0,
            duplicate_count,
            "0 duplicate symbols",
            provenance,
        ),
        _check(
            "contiguous_factor_rank",
            actual_ranks == expected_ranks,
            actual_ranks,
            str(expected_ranks),
            provenance,
        ),
        _check(
            "no_production_eligibility",
            frame.is_empty()
            or frame.filter(pl.col("production_eligible")).is_empty(),
            0
            if frame.is_empty()
            else frame.filter(pl.col("production_eligible")).height,
            "0 production-eligible rows while the factor branch is shadow-only",
            provenance,
        ),
        _check(
            "point_in_time_cutoff",
            frame.is_empty()
            or frame.filter(pl.col("data_cutoff_utc") > asof_utc).is_empty(),
            0
            if frame.is_empty()
            else frame.filter(pl.col("data_cutoff_utc") > asof_utc).height,
            "0 rows use data after the declared asof",
            provenance,
        ),
    )
    snapshot, path = persist_snapshot(
        frame,
        root=data_root,
        source=SOURCE,
        schema_version="factor_candidates_shadow.v1",
        checks=checks,
        parent_snapshot_ids=(
            daily_snapshot.dataset_id,
            rvol_snapshot.dataset_id,
        ),
    )
    snapshot.assert_usable()
    return frame, snapshot, path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_parse_date, required=True)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    args = parser.parse_args()

    daily_pool = load_premarket_pool(
        args.data_root,
        args.trade_date,
        pool="factor",
    )
    rvol, rvol_snapshot = _load_factor_rvol(args.data_root, args.trade_date)
    decision_values = rvol.get_column("decision_asof_utc").unique().to_list()
    if len(decision_values) != 1 or not isinstance(decision_values[0], datetime):
        raise ValueError("factor RVOL snapshot has an invalid decision asof")
    asof_utc = decision_values[0].astimezone(UTC)
    cfg = load_config(ROOT / "config.yaml")
    factor = cfg.factor_selection
    frame, snapshot, path = build_factor_candidate_snapshot(
        daily_pool.frame,
        rvol,
        daily_snapshot=daily_pool.snapshot,
        rvol_snapshot=rvol_snapshot,
        data_root=args.data_root,
        trade_date=args.trade_date,
        asof_utc=asof_utc,
        policy=FactorSelectionPolicy(
            min_rvol=cfg.universe.min_rvol,
            min_gap_return=cfg.universe.min_premarket_gap_return,
            min_score=factor.min_score,
            max_candidates=factor.max_candidates,
            rvol_full_score=factor.rvol_full_score,
            gap_full_score=factor.gap_full_score,
            premarket_return_full_score=factor.premarket_return_full_score,
            vwap_extension_full_score=factor.vwap_extension_full_score,
            beta_full_score=factor.beta_full_score,
            atr_full_score=factor.atr_full_score,
        ),
    )
    selected = frame.filter(pl.col("factor_pass")).sort("factor_rank")
    print(
        json.dumps(
            {
                "ok": True,
                "trade_date": args.trade_date.isoformat(),
                "status": "shadow_complete",
                "input_symbols": frame.height,
                "selected": selected.height,
                "selected_symbols": selected.get_column("symbol").to_list(),
                "production_eligible": False,
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
