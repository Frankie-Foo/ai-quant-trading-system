"""Run the fixed 10:00 soft-rejection recovery lane without orders."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, QualitySeverity
from data_plane.providers.alpaca import fetch_bars, stock_data_policy_from_env
from data_plane.quality import BAR_SCHEMA_VERSION, audit_minute_bars
from data_plane.storage import persist_snapshot
from operations.local_env import load_project_env, project_data_root
from research.selection_recovery import h30_recovery_features, recovery_reasons
from scripts.build_counterfactual_candidate_cohort import build_counterfactual_cohort
from scripts.build_h30_candidate_cohort import latest_gate_paths

ROOT = Path(__file__).resolve().parents[1]
BAR_SOURCE = "alpaca.sip.selection_recovery_1m"
DECISION_SOURCE = "research.selection_recovery_shadow"
RULE_VERSION = "selection_recovery_shadow.v1"


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("asof must be timezone-aware")
    return parsed.astimezone(UTC)


def _check(name: str, passed: bool, observed: object, expected: str) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=QualitySeverity.CRITICAL,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance=RULE_VERSION,
    )


def main() -> int:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", type=date.fromisoformat, required=True)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    parser.add_argument("--asof-utc", type=_parse_utc)
    args = parser.parse_args()
    asof_utc = args.asof_utc or datetime.now(UTC)
    forward_valid = (
        args.asof_utc is None
        and args.trade_date
        == asof_utc.astimezone(ZoneInfo("America/New_York")).date()
    )
    schedule = build_xnys_schedule(args.trade_date, args.trade_date)
    if schedule.height != 1:
        print(json.dumps({"status": "not_trading_day", "orders_submitted": 0}))
        return 0
    session = schedule.row(0, named=True)
    session_open = session["market_open_utc"]
    if not isinstance(session_open, datetime):
        raise ValueError("market open is invalid")
    decision_due = session_open + timedelta(minutes=35)
    if asof_utc < decision_due:
        print(json.dumps({"status": "not_due", "orders_submitted": 0}))
        return 0

    gate = latest_gate_paths(args.data_root).get(args.trade_date)
    if gate is None:
        raise FileNotFoundError("selection gate snapshot is missing")
    cohort, _ = build_counterfactual_cohort({args.trade_date: gate})
    rejected = cohort.filter(~pl.col("pass_gate"))
    if rejected.is_empty():
        print(json.dumps({"status": "no_soft_rejections", "orders_submitted": 0}))
        return 0

    symbols = tuple(sorted({"SPY", *rejected.get_column("symbol").to_list()}))
    cutoff = session_open + timedelta(minutes=30)
    policy = stock_data_policy_from_env()
    bars = fetch_bars(symbols, session_open, cutoff, feed=policy.feed)
    provenance = f"{BAR_SOURCE}@{args.trade_date}|cutoff={cutoff.isoformat()}"
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
        parent_snapshot_ids=(gate[1].dataset_id,),
    )
    bar_snapshot.assert_usable()
    spy = bars.filter(pl.col("symbol") == "SPY")
    rows: list[dict[str, object]] = []
    for row in rejected.iter_rows(named=True):
        features = h30_recovery_features(
            bars.filter(pl.col("symbol") == row["symbol"]),
            spy,
            session_open_utc=session_open,
        )
        reasons = (
            ("h30_data_incomplete",)
            if features is None
            else recovery_reasons(int(row["catalyst_tier"]), features)
        )
        rows.append(
            {
                "session_date": args.trade_date,
                "symbol": row["symbol"],
                "original_reject_reason": row["reject_reason"],
                "catalyst_tier": row["catalyst_tier"],
                "h30_return": features["h30_return"] if features else None,
                "h30_relative_spy": features["h30_relative_spy"] if features else None,
                "h30_close_location": features["h30_close_location"] if features else None,
                "h30_above_vwap": features["h30_above_vwap"] if features else None,
                "recovery_selected": not reasons,
                "recovery_reject_reasons": list(reasons),
                "decision_asof_utc": asof_utc,
                "market_data_cutoff_utc": cutoff,
                "forward_valid": forward_valid
                and decision_due <= asof_utc <= session_open + timedelta(minutes=45),
                "rule_version": RULE_VERSION,
                "orders_authorized": False,
                "production_eligible": False,
            }
        )
    decisions = pl.DataFrame(rows, infer_schema_length=None)
    snapshot, path = persist_snapshot(
        decisions,
        root=args.data_root,
        source=DECISION_SOURCE,
        schema_version=RULE_VERSION,
        checks=(
            _check("non_empty", decisions.height > 0, decisions.height, ">0"),
            _check("orders_disabled", True, False, "orders_authorized=false"),
            _check("production_ineligible", True, False, "false"),
        ),
        parent_snapshot_ids=(gate[1].dataset_id, bar_snapshot.dataset_id),
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "candidates": decisions.height,
                "selected": decisions.filter(pl.col("recovery_selected")).height,
                "forward_valid": bool(decisions.get_column("forward_valid").all()),
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
