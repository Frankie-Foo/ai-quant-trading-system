"""Prepare today's audited first-wave pool for modern momentum shadowing."""

from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from data_plane.contracts import DataQualityCheck, QualitySeverity
from data_plane.snapshot_queries import load_snapshot_by_id
from data_plane.storage import persist_snapshot
from operations.local_env import load_project_env, project_data_root
from research.modern_momentum_forward import select_forward_pool
from scripts.build_h30_candidate_cohort import latest_gate_paths

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "research.modern_momentum.forward_pool"


def _check(name: str, passed: bool, observed: object, expected: str) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=QualitySeverity.CRITICAL,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance="scripts.prepare_modern_momentum_forward.v1",
    )


def _current_sip_caps(gates: pl.DataFrame) -> dict[str, float]:
    """Use the gate's fresh SIP cap evidence, never a slow provider fallback."""
    required = {"symbol", "market_cap", "market_cap_provenance"}
    missing = required - set(gates.columns)
    if missing:
        raise ValueError(f"selection gates missing current market-cap columns: {sorted(missing)}")
    caps: dict[str, float] = {}
    for row in gates.iter_rows(named=True):
        value = row["market_cap"]
        if (
            isinstance(value, (int, float))
            and math.isfinite(float(value))
            and float(value) > 0
            and "alpaca.sip" in str(row["market_cap_provenance"])
        ):
            caps[str(row["symbol"]).strip().upper()] = float(value)
    return caps


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True, type=date.fromisoformat)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--all-eligible", action="store_true")
    parser.add_argument("--gate-snapshot")
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 50:
        raise ValueError("limit must be between 1 and 50")
    if args.gate_snapshot:
        frame, gate_snapshot = load_snapshot_by_id(
            args.data_root, args.gate_snapshot, source="kernel.universe.selection_gates",
            available_by=datetime.now(UTC),
        )
        if frame["session_date"].unique().to_list() != [args.trade_date]:
            raise ValueError("gate snapshot trade date mismatch")
    else:
        gates = latest_gate_paths(args.data_root)
        if args.trade_date not in gates:
            raise FileNotFoundError("selection gates are missing for trade date")
        gate_path, gate_snapshot = gates[args.trade_date]
        frame = pl.read_parquet(gate_path)
    previous_session = frame.get_column("asof_date").max()
    if not isinstance(previous_session, date) or previous_session >= args.trade_date:
        raise ValueError("previous session date is invalid")
    candidates = frame.filter(
        pl.col("pass_gate").fill_null(False) & (pl.col("rvol").fill_null(0) >= 1.5)
    )
    caps = _current_sip_caps(candidates)
    missing = tuple(sorted(set(candidates["symbol"]) - set(caps)))
    parent_ids = [gate_snapshot.dataset_id]
    if missing:
        raise RuntimeError(
            "selection gates contain candidates without current cached-shares × SIP market caps"
        )
    pool_limit = candidates.height if args.all_eligible else args.limit
    pool = select_forward_pool(candidates, market_caps=caps, limit=pool_limit)
    if pool.is_empty():
        raise RuntimeError("zero candidates satisfy modern momentum first-wave gates")
    minimum_cap = pool["forward_market_cap"].min()
    if not isinstance(minimum_cap, (int, float)):
        raise ValueError("forward pool market cap is invalid")
    snapshot, path = persist_snapshot(
        pool,
        root=args.data_root,
        source=SOURCE,
        schema_version="modern_momentum_forward_pool.v1",
        checks=(
            _check("non_empty", pool.height > 0, pool.height, ">0"),
            _check("maximum_rows", pool.height <= pool_limit, pool.height, f"<={pool_limit}"),
            _check(
                "minimum_market_cap",
                minimum_cap >= 1e9,
                minimum_cap,
                ">=1000000000",
            ),
        ),
        parent_snapshot_ids=tuple(parent_ids),
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "trade_date": args.trade_date.isoformat(),
                "symbols": pool["symbol"].to_list(),
                "rows": pool.select(
                    "forward_rank",
                    "symbol",
                    "forward_market_cap",
                    "rvol",
                    "premarket_return",
                    "catalyst_categories",
                ).to_dicts(),
                "market_cap_source": "selection_gate_current_sip",
                "market_cap_missing": len(missing),
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
                "production_eligible": False,
            },
            ensure_ascii=False,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
