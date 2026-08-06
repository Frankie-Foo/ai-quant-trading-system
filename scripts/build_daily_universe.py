from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from data_plane.contracts import DataQualityCheck, QualitySeverity
from data_plane.storage import persist_snapshot
from kernel.universe import build_universe

ROOT = Path(__file__).resolve().parents[1]


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _checks(frame: pl.DataFrame, target_date: date) -> tuple[DataQualityCheck, ...]:
    provenance = f"kernel.universe.daily_precheck@{target_date.isoformat()}"
    asof_dates = frame.get_column("asof_date").unique().to_list()
    duplicate_count = frame.height - frame.get_column("symbol").n_unique()
    wrong_types = frame.filter(pl.col("security_type") != "CS").height
    invalid_prices = frame.filter(
        pl.col("price").is_null() | ~pl.col("price").is_finite() | (pl.col("price") <= 0)
    ).height
    incorrectly_open = frame.filter(pl.col("pass_gate")).height
    missing_provenance = frame.filter(
        pl.col("price_provenance").is_null()
        | pl.col("security_type_provenance").is_null()
        | pl.col("identity_check_provenance").is_null()
    ).height
    valid_asof = (
        len(asof_dates) == 1
        and isinstance(asof_dates[0], date)
        and asof_dates[0] < target_date
    )
    return (
        DataQualityCheck(
            name="non_empty",
            severity=QualitySeverity.CRITICAL,
            passed=frame.height > 0,
            observed=str(frame.height),
            expected="row_count > 0",
            provenance=provenance,
        ),
        DataQualityCheck(
            name="unique_symbol",
            severity=QualitySeverity.CRITICAL,
            passed=duplicate_count == 0,
            observed=str(duplicate_count),
            expected="0 duplicate symbols",
            provenance=provenance,
        ),
        DataQualityCheck(
            name="point_in_time_asof",
            severity=QualitySeverity.CRITICAL,
            passed=valid_asof,
            observed=str(asof_dates),
            expected=f"one asof_date strictly before {target_date.isoformat()}",
            provenance=provenance,
        ),
        DataQualityCheck(
            name="common_stock_only",
            severity=QualitySeverity.CRITICAL,
            passed=wrong_types == 0,
            observed=str(wrong_types),
            expected="0 non-CS rows",
            provenance=provenance,
        ),
        DataQualityCheck(
            name="valid_price",
            severity=QualitySeverity.CRITICAL,
            passed=invalid_prices == 0,
            observed=str(invalid_prices),
            expected="0 null, non-finite, or non-positive prices",
            provenance=provenance,
        ),
        DataQualityCheck(
            name="incomplete_gate_fails_closed",
            severity=QualitySeverity.CRITICAL,
            passed=incorrectly_open == 0,
            observed=str(incorrectly_open),
            expected="0 pass_gate rows before RVOL/market-cap/earnings/LULD are complete",
            provenance=provenance,
        ),
        DataQualityCheck(
            name="decision_provenance_present",
            severity=QualitySeverity.CRITICAL,
            passed=missing_provenance == 0,
            observed=str(missing_provenance),
            expected="0 rows missing core provenance",
            provenance=provenance,
        ),
    )


def _manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"manifest root is not an object: {path}")
    return payload


def _parent_snapshot_ids(data_root: Path, target_date: date) -> tuple[str, ...]:
    daily: list[tuple[date, str]] = []
    for path in (data_root / "accepted").glob("massive.grouped_daily-*/data.parquet"):
        value = pl.read_parquet(path, columns=["trade_date"]).get_column("trade_date").min()
        if isinstance(value, date) and value < target_date:
            dataset_id = _manifest(path.parent / "manifest.json").get("dataset_id")
            if isinstance(dataset_id, str):
                daily.append((value, dataset_id))

    reference: list[tuple[date, str]] = []
    for path in (data_root / "accepted").glob(
        "massive.reference_tickers.cs-*/data.parquet"
    ):
        value = pl.read_parquet(path, columns=["asof_date"]).get_column("asof_date").max()
        if isinstance(value, date) and value < target_date:
            dataset_id = _manifest(path.parent / "manifest.json").get("dataset_id")
            if isinstance(dataset_id, str):
                reference.append((value, dataset_id))

    daily_ids = [item[1] for item in sorted(daily)[-300:]]
    if not daily_ids or not reference:
        raise ValueError("daily or common-stock parent snapshots are missing")
    return tuple(daily_ids + [max(reference)[1]])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_parse_date, required=True)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    args = parser.parse_args()

    frame = build_universe(args.trade_date, data_root=args.data_root)
    checks = _checks(frame, args.trade_date)
    snapshot, path = persist_snapshot(
        frame,
        root=args.data_root,
        source="kernel.universe.daily_precheck",
        schema_version="universe_daily_precheck.v2",
        checks=checks,
        parent_snapshot_ids=_parent_snapshot_ids(args.data_root, args.trade_date),
    )
    result = {
        "dataset_id": snapshot.dataset_id,
        "usable": snapshot.usable,
        "rows": snapshot.row_count,
        "precheck_pass": frame.filter(pl.col("precheck_pass")).height,
        "pass_gate": frame.filter(pl.col("pass_gate")).height,
        "identity_discontinuities": frame.filter(
            pl.col("max_abs_return") > 0.90
        ).height,
        "path": str(path),
        "failed_checks": [check.name for check in checks if not check.passed],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
