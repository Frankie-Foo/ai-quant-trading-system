"""Attach full-session outcomes to one recovery-shadow decision snapshot."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.providers.alpaca import fetch_bars, stock_data_policy_from_env
from data_plane.quality import BAR_SCHEMA_VERSION, audit_minute_bars
from data_plane.storage import persist_snapshot
from operations.local_env import load_project_env, project_data_root
from scripts.analyze_counterfactual_selection import candidate_outcome

ROOT = Path(__file__).resolve().parents[1]
DECISION_SOURCE = "research.selection_recovery_shadow"
BAR_SOURCE = "alpaca.sip.selection_recovery_outcome_1m"
OUTCOME_SOURCE = "research.selection_recovery_shadow_outcomes"


def _manifest(path: Path) -> DatasetSnapshot:
    return DatasetSnapshot.model_validate_json(
        (path.parent / "manifest.json").read_text(encoding="utf-8")
    )


def _latest_decision(
    data_root: Path, trade_date: date
) -> tuple[pl.DataFrame, DatasetSnapshot]:
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(f"{DECISION_SOURCE}-*/data.parquet"):
        frame = pl.read_parquet(path, columns=["session_date"])
        if frame.get_column("session_date").unique().to_list() != [trade_date]:
            continue
        snapshot = _manifest(path)
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        raise FileNotFoundError("selection recovery decision is missing")
    _, path, snapshot = max(matches, key=lambda item: item[0])
    return pl.read_parquet(path), snapshot


def _check(name: str, passed: bool, observed: object, expected: str) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=QualitySeverity.CRITICAL,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance="scripts.label_selection_recovery_shadow.v1",
    )


def main() -> int:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", type=date.fromisoformat, required=True)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    args = parser.parse_args()
    decisions, decision_snapshot = _latest_decision(args.data_root, args.trade_date)
    schedule = build_xnys_schedule(args.trade_date, args.trade_date)
    if schedule.height != 1:
        raise ValueError("trade date is not an XNYS session")
    session = schedule.row(0, named=True)
    session_open = session["market_open_utc"]
    session_close = session["market_close_utc"]
    symbols = tuple(sorted({"SPY", *decisions.get_column("symbol").to_list()}))
    policy = stock_data_policy_from_env()
    bars = fetch_bars(symbols, session_open, session_close, feed=policy.feed)
    provenance = f"{BAR_SOURCE}@{args.trade_date}"
    bar_snapshot, _ = persist_snapshot(
        bars,
        root=args.data_root,
        source=BAR_SOURCE,
        schema_version=BAR_SCHEMA_VERSION,
        checks=audit_minute_bars(
            bars,
            provenance=provenance,
            expected_symbols=symbols,
            research_approved=policy.feed == "sip" and policy.is_realtime,
        ),
        parent_snapshot_ids=(decision_snapshot.dataset_id,),
    )
    bar_snapshot.assert_usable()
    spy = bars.filter(pl.col("symbol") == "SPY")
    rows: list[dict[str, object]] = []
    for row in decisions.iter_rows(named=True):
        outcome = candidate_outcome(
            bars.filter(pl.col("symbol") == row["symbol"]),
            spy,
            session_open_utc=session_open,
        )
        rows.append(
            {
                "session_date": args.trade_date,
                "symbol": row["symbol"],
                "recovery_selected": row["recovery_selected"],
                "forward_valid": row["forward_valid"],
                "outcome_status": "complete" if outcome is not None else "blocked",
                "forward_mfe_pct": outcome["forward_mfe_pct"] if outcome else None,
                "forward_mae_pct": outcome["forward_mae_pct"] if outcome else None,
                "forward_close_pct": outcome["forward_close_pct"] if outcome else None,
                "clean_three_percent_opportunity": (
                    outcome is not None
                    and outcome["forward_mfe_pct"] >= 0.03
                    and outcome["forward_mae_pct"] > -0.02
                ),
                "orders_authorized": False,
                "production_eligible": False,
            }
        )
    outcomes = pl.DataFrame(rows, infer_schema_length=None)
    snapshot, path = persist_snapshot(
        outcomes,
        root=args.data_root,
        source=OUTCOME_SOURCE,
        schema_version="selection_recovery_shadow_outcomes.v1",
        checks=(
            _check("non_empty", outcomes.height > 0, outcomes.height, ">0"),
            _check("orders_disabled", True, False, "orders_authorized=false"),
            _check("production_ineligible", True, False, "false"),
        ),
        parent_snapshot_ids=(decision_snapshot.dataset_id, bar_snapshot.dataset_id),
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "labels": outcomes.height,
                "forward_valid_labels": outcomes.filter(pl.col("forward_valid")).height,
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
                "orders_submitted": 0,
                "production_eligible": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
