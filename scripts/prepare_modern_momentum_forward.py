"""Prepare today's audited first-wave pool for modern momentum shadowing."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import polars as pl

from data_plane.contracts import DataQualityCheck, QualitySeverity
from data_plane.providers.massive import fetch_ticker_details
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


def _latest_caps(data_root: Path, *, asof: date) -> dict[str, float]:
    rows: list[dict[str, object]] = []
    for path in (data_root / "accepted").glob("massive.ticker_details-*/data.parquet"):
        frame = pl.read_parquet(path).filter(
            (pl.col("asof_date") <= asof) & pl.col("market_cap").is_not_null()
        )
        rows.extend(frame.select("symbol", "asof_date", "market_cap").to_dicts())
    latest: dict[str, tuple[date, float]] = {}
    for row in rows:
        symbol = str(row["symbol"])
        row_date = row["asof_date"]
        cap = row["market_cap"]
        if not isinstance(row_date, date) or not isinstance(cap, (int, float)):
            continue
        if symbol not in latest or row_date > latest[symbol][0]:
            latest[symbol] = (row_date, float(cap))
    return {symbol: item[1] for symbol, item in latest.items()}


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True, type=date.fromisoformat)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    args = parser.parse_args()
    gates = latest_gate_paths(args.data_root)
    if args.trade_date not in gates:
        raise FileNotFoundError("selection gates are missing for trade date")
    gate_path, gate_snapshot = gates[args.trade_date]
    frame = pl.read_parquet(gate_path)
    previous_session = frame.get_column("asof_date").max()
    if not isinstance(previous_session, date) or previous_session >= args.trade_date:
        raise ValueError("previous session date is invalid")
    candidates = frame.filter(pl.col("rvol").fill_null(0) >= 1.5)
    caps = _latest_caps(args.data_root, asof=previous_session)
    missing = tuple(sorted(set(candidates["symbol"]) - set(caps)))
    parent_ids = [gate_snapshot.dataset_id]
    if missing:
        fetched = fetch_ticker_details(missing, previous_session)
        fetched_snapshot, _ = persist_snapshot(
            fetched,
            root=args.data_root,
            source="massive.ticker_details",
            schema_version="ticker_details.v1",
            checks=(
                _check(
                    "requested_symbols_returned",
                    fetched.height == len(missing),
                    fetched.height,
                    str(len(missing)),
                ),
            ),
            parent_snapshot_ids=(gate_snapshot.dataset_id,),
        )
        fetched_snapshot.assert_usable()
        parent_ids.append(fetched_snapshot.dataset_id)
        for row in fetched.iter_rows(named=True):
            if isinstance(row["market_cap"], (int, float)):
                caps[str(row["symbol"])] = float(row["market_cap"])
    pool = select_forward_pool(frame, market_caps=caps)
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
            _check("maximum_ten", pool.height <= 10, pool.height, "<=10"),
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
                "missing_market_caps_fetched": len(missing),
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
