"""Persist the unified catalyst/factor/order-flow shadow ranking."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.snapshot_queries import load_latest_session_snapshot
from data_plane.storage import persist_snapshot
from kernel.config import load_config
from kernel.selection_arbitration import (
    ShadowArbitrationPolicy,
    arbitrate_shadow_candidates,
)

ROOT = Path(__file__).resolve().parents[1]
CATALYST_SOURCE = "kernel.universe.selection_gates"
FACTOR_SOURCE = "kernel.selection.factor_candidates_shadow"
ORDER_FLOW_SOURCE = "kernel.features.order_flow_shadow"
SOURCE = "kernel.selection.unified_shadow"


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


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


def build_unified_shadow_snapshot(
    catalyst: pl.DataFrame,
    factor: pl.DataFrame,
    order_flow: pl.DataFrame,
    *,
    catalyst_snapshot: DatasetSnapshot,
    factor_snapshot: DatasetSnapshot,
    order_flow_snapshot: DatasetSnapshot,
    data_root: Path,
    trade_date: date,
    asof_utc: datetime,
    policy: ShadowArbitrationPolicy,
) -> tuple[pl.DataFrame, DatasetSnapshot, Path]:
    """Persist a ranked research result with lineage to all three input modules."""

    frame = arbitrate_shadow_candidates(
        catalyst,
        factor,
        order_flow,
        asof_utc=asof_utc,
        policy=policy,
    ).with_columns(pl.lit(trade_date).alias("session_date"))
    duplicate_count = (
        0
        if frame.is_empty()
        else frame.height - frame.get_column("symbol").n_unique()
    )
    actual_ranks = (
        frame.sort("unified_rank").get_column("unified_rank").to_list()
        if not frame.is_empty()
        else []
    )
    expected_ranks = list(range(1, frame.height + 1))
    unsafe_count = (
        0
        if frame.is_empty()
        else frame.filter(
            pl.col("production_eligible") | pl.col("execution_eligible")
        ).height
    )
    future_count = (
        0
        if frame.is_empty()
        else frame.filter(pl.col("arbitration_asof_utc") > asof_utc).height
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
            "contiguous_unified_rank",
            actual_ranks == expected_ranks,
            actual_ranks,
            str(expected_ranks),
            provenance,
        ),
        _check(
            "shadow_only",
            unsafe_count == 0,
            unsafe_count,
            "0 production/execution-eligible rows",
            provenance,
        ),
        _check(
            "point_in_time_cutoff",
            future_count == 0,
            future_count,
            "0 rows after declared asof",
            provenance,
        ),
    )
    snapshot, path = persist_snapshot(
        frame,
        root=data_root,
        source=SOURCE,
        schema_version="unified_shadow_selection.v1",
        checks=checks,
        parent_snapshot_ids=(
            catalyst_snapshot.dataset_id,
            factor_snapshot.dataset_id,
            order_flow_snapshot.dataset_id,
        ),
    )
    snapshot.assert_usable()
    return frame, snapshot, path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_parse_date, required=True)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    args = parser.parse_args()

    catalyst, catalyst_snapshot = load_latest_session_snapshot(
        args.data_root,
        source=CATALYST_SOURCE,
        session_date=args.trade_date,
    )
    factor, factor_snapshot = load_latest_session_snapshot(
        args.data_root,
        source=FACTOR_SOURCE,
        session_date=args.trade_date,
    )
    order_flow, order_flow_snapshot = load_latest_session_snapshot(
        args.data_root,
        source=ORDER_FLOW_SOURCE,
        session_date=args.trade_date,
    )
    cutoffs = order_flow.get_column("data_cutoff_utc").unique().to_list()
    if len(cutoffs) != 1 or not isinstance(cutoffs[0], datetime):
        raise ValueError("order-flow snapshot must have exactly one data cutoff")
    asof_utc = cutoffs[0].astimezone(UTC)
    cfg = load_config(ROOT / "config.yaml").shadow_arbitration
    frame, snapshot, path = build_unified_shadow_snapshot(
        catalyst,
        factor,
        order_flow,
        catalyst_snapshot=catalyst_snapshot,
        factor_snapshot=factor_snapshot,
        order_flow_snapshot=order_flow_snapshot,
        data_root=args.data_root,
        trade_date=args.trade_date,
        asof_utc=asof_utc,
        policy=ShadowArbitrationPolicy(
            intersection_bonus=cfg.intersection_bonus,
            order_flow_weight=cfg.order_flow_weight,
            max_order_flow_adjustment=cfg.max_order_flow_adjustment,
            max_candidates=cfg.max_candidates,
        ),
    )
    print(
        json.dumps(
            {
                "ok": True,
                "status": "shadow_complete",
                "trade_date": args.trade_date.isoformat(),
                "asof_utc": asof_utc.isoformat(),
                "ranked": frame.height,
                "top_symbols": frame.head(10).get_column("symbol").to_list(),
                "production_eligible": False,
                "execution_eligible": False,
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
