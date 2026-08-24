"""Replay frozen pullback-acceptance v1 against the governed H30 cohort."""

from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.storage import persist_snapshot
from research.pullback_acceptance import (
    PullbackAcceptanceResult,
    evaluate_pullback_acceptance,
)

ROOT = Path(__file__).resolve().parents[1]
COHORT_SOURCE = "research.h30_candidate_cohort"
CENSUS_SOURCE = "research.pullback_acceptance.census"
LABEL_SOURCE = "research.pullback_acceptance.labels"
METRIC_SOURCE = "research.pullback_acceptance.metrics"
DECISION_SOURCE = "research.pullback_acceptance.decision"
RISK_BUDGET_USD = 5_000.0
DAILY_RISK_LIMIT_USD = 30_000.0
DAILY_NOTIONAL_LIMIT_USD = 2_000_000.0
COMMISSION_PER_SHARE_ROUND_TRIP = 0.007


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
        raise FileNotFoundError("H30 candidate cohort is missing")
    _, path, snapshot = max(matches, key=lambda item: item[0])
    return pl.read_parquet(path), snapshot


def _rth_by_date(
    data_root: Path,
) -> tuple[dict[date, pl.DataFrame], tuple[str, ...]]:
    frames: dict[date, pl.DataFrame] = {}
    parents: dict[date, str] = {}
    stamps: dict[date, datetime] = {}
    for path in (data_root / "accepted").glob("alpaca.sip.rth_1m-*/data.parquet"):
        frame = pl.read_parquet(path)
        dates = (
            frame.get_column("ts_utc")
            .dt.convert_time_zone("America/New_York")
            .dt.date()
            .unique()
            .to_list()
        )
        if len(dates) != 1:
            continue
        snapshot = _manifest(path)
        if dates[0] not in stamps or stamps[dates[0]] < snapshot.asof_utc:
            frames[dates[0]] = frame
            parents[dates[0]] = snapshot.dataset_id
            stamps[dates[0]] = snapshot.asof_utc
    return frames, tuple(parents.values())


def _check(name: str, passed: bool, observed: Any, expected: str) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=QualitySeverity.CRITICAL,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance="scripts.run_pullback_acceptance_backtest.v1",
    )


def _metrics(labels: pl.DataFrame) -> dict[str, object]:
    if labels.is_empty():
        return {
            "trade_legs": 0,
            "net_pnl": 0.0,
            "win_rate": None,
            "profit_factor": None,
            "max_drawdown_usd": 0.0,
            "positive_folds": 0,
        }
    pnl = labels.get_column("net_pnl")
    gains = float(pnl.filter(pnl > 0).sum() or 0)
    losses = abs(float(pnl.filter(pnl < 0).sum() or 0))
    daily = labels.group_by("trade_date").agg(pl.col("net_pnl").sum()).sort("trade_date")
    curve = np.cumsum(daily.get_column("net_pnl").to_numpy())
    peak = np.maximum.accumulate(np.insert(curve, 0, 0.0))[1:]
    blocks = np.array_split(daily.get_column("net_pnl").to_numpy(), 5)
    return {
        "trade_legs": labels.height,
        "net_pnl": float(pnl.sum()),
        "win_rate": labels.filter(pl.col("net_pnl") > 0).height / labels.height,
        "profit_factor": gains / losses if losses else None,
        "max_drawdown_usd": float((curve - peak).min()),
        "positive_folds": sum(float(block.sum()) > 0 for block in blocks if block.size),
    }


def _number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    return float(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", type=Path, default=ROOT / "runtime" / "ai-quant" / "data"
    )
    args = parser.parse_args()
    cohort, cohort_snapshot = _latest_cohort(args.data_root)
    bars_by_date, bar_parents = _rth_by_date(args.data_root)
    start = cohort.get_column("session_date").min()
    end = cohort.get_column("session_date").max()
    if not isinstance(start, date) or not isinstance(end, date):
        raise ValueError("cohort date range is invalid")
    schedule = {
        row["trade_date"]: row
        for row in build_xnys_schedule(start, end).iter_rows(named=True)
    }

    census_rows: list[dict[str, object]] = []
    trades: dict[date, list[tuple[int, PullbackAcceptanceResult]]] = {}
    for row in cohort.sort("session_date", "selection_rank", "symbol").iter_rows(
        named=True
    ):
        day = row["session_date"]
        symbol = str(row["symbol"])
        session = schedule.get(day)
        bars = bars_by_date.get(day, pl.DataFrame()).filter(pl.col("symbol") == symbol)
        if session is None or bars.is_empty():
            continue
        result = evaluate_pullback_acceptance(
            bars, session_open_utc=session["market_open_utc"]
        )
        census_rows.append(
            {
                "trade_date": day,
                "symbol": symbol,
                "selection_rank": int(row["selection_rank"]),
                "status": result.status,
                "reason": result.reason,
                "h30": result.h30,
                "l30": result.l30,
                "breakout_ts_utc": result.breakout_ts_utc,
                "pullback_ts_utc": result.pullback_ts_utc,
                "entry_ts_utc": result.entry_ts_utc,
                "pullback_volume_ratio": result.pullback_volume_ratio,
                "vwap_slope": result.vwap_slope,
                "provenance": result.provenance,
            }
        )
        if result.status == "traded" and result.leg is not None:
            trades.setdefault(day, []).append((int(row["selection_rank"]), result))

    label_rows: list[dict[str, object]] = []
    for day, candidates in sorted(trades.items()):
        remaining_notional = DAILY_NOTIONAL_LIMIT_USD
        used_risk = 0.0
        for rank, result in sorted(candidates, key=lambda item: item[0])[:3]:
            leg = result.leg
            if leg is None or used_risk + RISK_BUDGET_USD > DAILY_RISK_LIMIT_USD:
                continue
            shares = math.floor(
                min(
                    RISK_BUDGET_USD / (leg.entry_px * 0.02),
                    remaining_notional / leg.entry_px,
                )
            )
            if shares <= 0:
                continue
            notional = shares * leg.entry_px
            commission = shares * COMMISSION_PER_SHARE_ROUND_TRIP
            net_pnl = shares * (leg.exit_px - leg.entry_px) - commission
            label_rows.append(
                {
                    "trade_date": day,
                    "symbol": result.symbol,
                    "selection_rank": rank,
                    "entry_ts_utc": leg.entry_ts_utc,
                    "entry_px": leg.entry_px,
                    "exit_ts_utc": leg.exit_ts_utc,
                    "exit_px": leg.exit_px,
                    "exit_reason": leg.exit_reason,
                    "shares": shares,
                    "risk_budget_usd": RISK_BUDGET_USD,
                    "notional_usd": notional,
                    "commission_usd": commission,
                    "return_pct": leg.return_pct,
                    "net_pnl": net_pnl,
                    "cost_status": "fixed_slippage_reserve_no_nbbo",
                    "production_eligible": False,
                }
            )
            remaining_notional -= notional
            used_risk += RISK_BUDGET_USD

    census = pl.DataFrame(census_rows, infer_schema_length=None)
    labels = pl.DataFrame(label_rows, infer_schema_length=None) if label_rows else pl.DataFrame()
    parents = (cohort_snapshot.dataset_id, *bar_parents)
    census_snapshot, _ = persist_snapshot(
        census,
        root=args.data_root,
        source=CENSUS_SOURCE,
        schema_version="pullback_acceptance_census.v1",
        checks=(
            _check("non_empty", census.height > 0, census.height, ">0"),
            _check(
                "unique_candidate_day",
                census.select("trade_date", "symbol").unique().height == census.height,
                census.height,
                "one row per candidate day",
            ),
        ),
        parent_snapshot_ids=parents,
    )
    census_snapshot.assert_usable()
    metric = _metrics(labels)
    label_snapshot, _ = persist_snapshot(
        labels,
        root=args.data_root,
        source=LABEL_SOURCE,
        schema_version="pullback_acceptance_labels.v1",
        checks=(
            _check("research_only", True, False, "production_eligible=false"),
        ),
        parent_snapshot_ids=(census_snapshot.dataset_id,),
    )
    label_snapshot.assert_usable()
    metric_frame = pl.DataFrame([{**metric, "production_eligible": False}])
    metric_snapshot, metric_path = persist_snapshot(
        metric_frame,
        root=args.data_root,
        source=METRIC_SOURCE,
        schema_version="pullback_acceptance_metrics.v1",
        checks=(_check("research_only", True, False, "production_eligible=false"),),
        parent_snapshot_ids=(label_snapshot.dataset_id,),
    )
    metric_snapshot.assert_usable()
    reasons: list[str] = []
    if int(_number(metric["trade_legs"], "trade_legs")) < 20:
        reasons.append("fewer_than_20_trade_legs")
    if _number(metric["net_pnl"], "net_pnl") <= 0:
        reasons.append("non_positive_net_pnl")
    profit_factor = metric["profit_factor"]
    if not isinstance(profit_factor, (int, float)) or profit_factor < 1.1:
        reasons.append("profit_factor_below_1_1")
    if int(_number(metric["positive_folds"], "positive_folds")) < 4:
        reasons.append("fewer_than_four_positive_folds")
    reasons.append("historical_nbbo_costs_missing")
    decision = pl.DataFrame(
        [
            {
                "strategy": "pullback_acceptance_v1",
                "status": "rejected",
                "reasons": reasons,
                "production_eligible": False,
            }
        ]
    )
    decision_snapshot, decision_path = persist_snapshot(
        decision,
        root=args.data_root,
        source=DECISION_SOURCE,
        schema_version="pullback_acceptance_decision.v1",
        checks=(_check("production_ineligible", True, False, "false"),),
        parent_snapshot_ids=(metric_snapshot.dataset_id,),
    )
    decision_snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "candidates": census.height,
                "status_counts": census.group_by("status").len().to_dicts(),
                "metrics": metric,
                "reasons": reasons,
                "metric_path": str(metric_path),
                "decision_path": str(decision_path),
                "production_eligible": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
