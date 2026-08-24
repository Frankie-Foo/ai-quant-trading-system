"""Bounded train/holdout search for one pullback-acceptance strategy."""

from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

import numpy as np
import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, QualitySeverity
from data_plane.storage import persist_snapshot
from research.pullback_acceptance import (
    PullbackAcceptanceConfig,
    PullbackAcceptanceResult,
    evaluate_pullback_acceptance,
)
from scripts.run_pullback_acceptance_backtest import _latest_cohort, _rth_by_date

ROOT = Path(__file__).resolve().parents[1]
SEARCH_SOURCE = "research.pullback_acceptance.search"
HOLDOUT_SOURCE = "research.pullback_acceptance.holdout"
DECISION_SOURCE = "research.pullback_acceptance.holdout_decision"


def _check(name: str, passed: bool, observed: object, expected: str) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=QualitySeverity.CRITICAL,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance="scripts.run_pullback_acceptance_search.v1",
    )


def strategy_metrics(returns: list[float]) -> dict[str, float | int | None]:
    if not returns:
        return {
            "trades": 0,
            "win_rate": None,
            "average_win_loss_ratio": None,
            "profit_factor": None,
            "net_return": 0.0,
            "max_drawdown": 0.0,
        }
    values = np.asarray(returns, dtype=float)
    wins = values[values > 0]
    losses = values[values < 0]
    average_loss = abs(float(losses.mean())) if losses.size else 0.0
    curve = np.cumsum(values)
    peak = np.maximum.accumulate(np.insert(curve, 0, 0.0))[1:]
    return {
        "trades": len(returns),
        "win_rate": float((values > 0).mean()),
        "average_win_loss_ratio": (
            float(wins.mean()) / average_loss if wins.size and average_loss else None
        ),
        "profit_factor": (
            float(wins.sum()) / abs(float(losses.sum()))
            if wins.size and losses.size
            else None
        ),
        "net_return": float(values.sum()),
        "max_drawdown": float((curve - peak).min()),
    }


def _replay(
    cohort: pl.DataFrame,
    bars_by_date: dict[date, pl.DataFrame],
    schedule: dict[date, dict[str, object]],
    *,
    dates: set[date],
    config: PullbackAcceptanceConfig,
) -> tuple[dict[str, float | int | None], list[dict[str, object]]]:
    by_day: dict[date, list[tuple[int, PullbackAcceptanceResult]]] = {}
    for row in cohort.filter(pl.col("session_date").is_in(dates)).iter_rows(named=True):
        day = row["session_date"]
        symbol = str(row["symbol"])
        session = schedule.get(day)
        bars = bars_by_date.get(day, pl.DataFrame()).filter(pl.col("symbol") == symbol)
        if session is None or bars.is_empty():
            continue
        session_open = session["market_open_utc"]
        if not isinstance(session_open, datetime):
            raise ValueError("market open must be a datetime")
        result = evaluate_pullback_acceptance(
            bars,
            session_open_utc=session_open,
            config=config,
        )
        if result.status == "traded" and result.leg is not None:
            by_day.setdefault(day, []).append((int(row["selection_rank"]), result))
    labels: list[dict[str, object]] = []
    returns: list[float] = []
    for day, candidates in sorted(by_day.items()):
        for rank, result in sorted(candidates, key=lambda item: item[0])[:3]:
            leg = result.leg
            if leg is None:
                continue
            net_return = leg.return_pct - 0.007 / leg.entry_px
            returns.append(net_return)
            labels.append(
                {
                    "trade_date": day,
                    "symbol": result.symbol,
                    "selection_rank": rank,
                    "entry_ts_utc": leg.entry_ts_utc,
                    "entry_px": leg.entry_px,
                    "exit_ts_utc": leg.exit_ts_utc,
                    "exit_px": leg.exit_px,
                    "exit_reason": leg.exit_reason,
                    "net_return": net_return,
                    "production_eligible": False,
                }
            )
    return strategy_metrics(returns), labels


def _number(metrics: dict[str, float | int | None], key: str) -> float:
    value = metrics[key]
    return float(value) if isinstance(value, (int, float)) else float("-inf")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", type=Path, default=ROOT / "runtime" / "ai-quant" / "data"
    )
    args = parser.parse_args()
    cohort, cohort_snapshot = _latest_cohort(args.data_root)
    bars_by_date, bar_parents = _rth_by_date(args.data_root)
    dates = sorted(cohort.get_column("session_date").unique().to_list())
    midpoint = len(dates) // 2
    train_dates = set(dates[:midpoint])
    holdout_dates = set(dates[midpoint:])
    schedule = {
        row["trade_date"]: row
        for row in build_xnys_schedule(dates[0], dates[-1]).iter_rows(named=True)
    }

    rows: list[dict[str, object]] = []
    configurations: list[PullbackAcceptanceConfig] = []
    for breakout, support, pullback, target in itertools.product(
        (0.5, 0.75), (0.005, 0.015), (0.8, 1.0), (1.2, 1.5)
    ):
        config = PullbackAcceptanceConfig(
            breakout_volume_ratio=breakout,
            support_tolerance_pct=support,
            pullback_volume_ratio=pullback,
            reclaim_volume_ratio=1.0,
            minimum_close_location=0.55,
            take_profit_r=target,
            allow_two_bar_acceptance=True,
        )
        metrics, _ = _replay(
            cohort, bars_by_date, schedule, dates=train_dates, config=config
        )
        configurations.append(config)
        rows.append(
            {
                "configuration": len(configurations) - 1,
                **asdict(config),
                **{f"train_{key}": value for key, value in metrics.items()},
                "production_eligible": False,
            }
        )
    search = pl.DataFrame(rows, infer_schema_length=None)
    eligible = search.filter(
        (pl.col("train_trades") >= 10)
        & (pl.col("train_win_rate") > 0.5)
        & (pl.col("train_average_win_loss_ratio") > 1.2)
        & (pl.col("train_profit_factor") > 1.2)
    )
    ranked = eligible if not eligible.is_empty() else search.filter(pl.col("train_trades") >= 10)
    if ranked.is_empty():
        ranked = search
    selected_row = ranked.sort(
        "train_profit_factor", "train_net_return", descending=True, nulls_last=True
    ).row(0, named=True)
    selected_index = int(selected_row["configuration"])
    selected = configurations[selected_index]
    holdout_metrics, holdout_labels = _replay(
        cohort, bars_by_date, schedule, dates=holdout_dates, config=selected
    )

    parents = (cohort_snapshot.dataset_id, *bar_parents)
    search_snapshot, search_path = persist_snapshot(
        search,
        root=args.data_root,
        source=SEARCH_SOURCE,
        schema_version="pullback_acceptance_search.v1",
        checks=(
            _check("fixed_search_budget", search.height == 16, search.height, "16"),
            _check("research_only", True, False, "production_eligible=false"),
        ),
        parent_snapshot_ids=parents,
    )
    search_snapshot.assert_usable()
    holdout = pl.DataFrame(holdout_labels, infer_schema_length=None)
    holdout_snapshot, holdout_path = persist_snapshot(
        holdout,
        root=args.data_root,
        source=HOLDOUT_SOURCE,
        schema_version="pullback_acceptance_holdout.v1",
        checks=(_check("research_only", True, False, "production_eligible=false"),),
        parent_snapshot_ids=(search_snapshot.dataset_id,),
    )
    holdout_snapshot.assert_usable()

    reasons: list[str] = []
    if _number(holdout_metrics, "trades") < 20:
        reasons.append("fewer_than_20_holdout_trades")
    if _number(holdout_metrics, "win_rate") <= 0.5:
        reasons.append("holdout_win_rate_not_above_50pct")
    if _number(holdout_metrics, "average_win_loss_ratio") <= 1.2:
        reasons.append("holdout_average_win_loss_not_above_1_2")
    if _number(holdout_metrics, "profit_factor") <= 1.2:
        reasons.append("holdout_profit_factor_not_above_1_2")
    reasons.append("historical_nbbo_costs_missing")
    decision = pl.DataFrame(
        [
            {
                "selected_configuration": selected_index,
                "train_start": min(train_dates),
                "train_end": max(train_dates),
                "holdout_start": min(holdout_dates),
                "holdout_end": max(holdout_dates),
                **{f"holdout_{key}": value for key, value in holdout_metrics.items()},
                "status": "rejected" if reasons else "sandbox_passed",
                "reasons": reasons,
                "production_eligible": False,
            }
        ],
        infer_schema_length=None,
    )
    decision_snapshot, decision_path = persist_snapshot(
        decision,
        root=args.data_root,
        source=DECISION_SOURCE,
        schema_version="pullback_acceptance_holdout_decision.v1",
        checks=(_check("production_ineligible", True, False, "false"),),
        parent_snapshot_ids=(holdout_snapshot.dataset_id,),
    )
    decision_snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "selected_configuration": selected_index,
                "selected": asdict(selected),
                "train": {
                    key.removeprefix("train_"): value
                    for key, value in selected_row.items()
                    if key.startswith("train_")
                },
                "holdout": holdout_metrics,
                "reasons": reasons,
                "search_path": str(search_path),
                "holdout_path": str(holdout_path),
                "decision_path": str(decision_path),
                "production_eligible": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
