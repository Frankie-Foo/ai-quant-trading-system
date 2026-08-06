"""Deterministic allowlisted RVOL Champion/Challenger research evaluation."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

from research.validation import purged_walk_forward_splits


@dataclass(frozen=True)
class RvolChampionDecision:
    baseline: float
    selected: float
    status: str
    discovery_improvement: float | None
    holdout_improvement: float | None
    attempted_configurations: int
    production_eligible: bool
    reason: str


def _required_float(value: object) -> float:
    if not isinstance(value, (int, float)):
        raise ValueError("expected a numeric metric")
    return float(value)


def _portfolio(frame: pl.DataFrame, threshold: float, max_concurrent: int) -> pl.DataFrame:
    return (
        frame.filter(pl.col("rvol") > threshold)
        .sort("trade_date", "selection_rank", "symbol")
        .group_by("trade_date", maintain_order=True)
        .head(max_concurrent)
    )


def evaluate_rvol_challengers(
    labels: pl.DataFrame,
    *,
    baseline: float,
    challengers: tuple[float, ...] = (3.5, 4.0, 5.0),
    max_concurrent: int = 8,
) -> tuple[pl.DataFrame, RvolChampionDecision]:
    required = {
        "trade_date",
        "symbol",
        "selection_rank",
        "rvol",
        "net_pnl",
        "net_return_on_notional",
    }
    missing = required - set(labels.columns)
    if missing:
        raise ValueError(f"labels missing required columns: {sorted(missing)}")
    if labels.is_empty() or max_concurrent <= 0:
        raise ValueError("labels must be non-empty and max_concurrent positive")
    if not challengers or any(value <= baseline for value in challengers):
        raise ValueError("challengers must be non-empty and strictly above baseline")
    ordered = labels.sort("trade_date", "selection_rank", "symbol")
    folds = purged_walk_forward_splits(
        np.array(ordered["trade_date"].to_list(), dtype=object),
        n_splits=5,
        purge_days=1,
        embargo_days=2,
    )
    thresholds = (baseline, *challengers)
    rows: list[dict[str, object]] = []
    for fold_number, fold in enumerate(folds, start=1):
        validation = (
            ordered.with_row_index("_row")
            .filter(pl.col("_row").is_in(fold.validation_indices.tolist()))
            .drop("_row")
        )
        for threshold in thresholds:
            portfolio = _portfolio(validation, threshold, max_concurrent)
            mean_return = (
                None
                if portfolio.is_empty()
                else _required_float(portfolio["net_return_on_notional"].mean())
            )
            rows.append(
                {
                    "fold": fold_number,
                    "threshold": threshold,
                    "validation_start": fold.validation_start,
                    "validation_end": fold.validation_end,
                    "trade_count": portfolio.height,
                    "mean_net_return": mean_return,
                    "net_pnl": float(portfolio["net_pnl"].sum() or 0.0),
                    "win_rate": (
                        None
                        if portfolio.is_empty()
                        else portfolio.filter(pl.col("net_pnl") > 0).height
                        / portfolio.height
                    ),
                }
            )
    metrics = pl.DataFrame(rows).with_columns(
        pl.col("validation_start").cast(pl.Date),
        pl.col("validation_end").cast(pl.Date),
    )
    baseline_by_fold = {
        int(row["fold"]): row
        for row in metrics.filter(pl.col("threshold") == baseline).iter_rows(named=True)
    }
    discovery_candidates: list[tuple[float, float]] = []
    for threshold in challengers:
        challenger_rows = tuple(
            metrics.filter(pl.col("threshold") == threshold).iter_rows(named=True)
        )
        discovery = challenger_rows[:4]
        baseline_discovery = tuple(baseline_by_fold[index] for index in range(1, 5))
        counts = [int(row["trade_count"]) for row in discovery]
        returns = [row["mean_net_return"] for row in discovery]
        baseline_returns = [row["mean_net_return"] for row in baseline_discovery]
        if (
            sum(counts) < 24
            or min(counts, default=0) < 3
            or any(value is None for value in (*returns, *baseline_returns))
        ):
            continue
        candidate_mean = sum(
            float(row["mean_net_return"]) * int(row["trade_count"])
            for row in discovery
        ) / sum(counts)
        baseline_count = sum(int(row["trade_count"]) for row in baseline_discovery)
        baseline_mean = sum(
            float(row["mean_net_return"]) * int(row["trade_count"])
            for row in baseline_discovery
        ) / baseline_count
        fold_wins = sum(
            float(candidate) > float(control)
            for candidate, control in zip(returns, baseline_returns, strict=True)
        )
        retention = sum(counts) / baseline_count
        improvement = candidate_mean - baseline_mean
        if (
            fold_wins >= 3
            and retention >= 0.35
            and candidate_mean > 0
            and improvement >= 0.001
        ):
            discovery_candidates.append((threshold, improvement))

    if not discovery_candidates:
        return metrics, RvolChampionDecision(
            baseline=baseline,
            selected=baseline,
            status="champion_retained",
            discovery_improvement=None,
            holdout_improvement=None,
            attempted_configurations=len(challengers),
            production_eligible=False,
            reason="no challenger passed the four-fold discovery gates",
        )
    selected, discovery_improvement = max(
        discovery_candidates, key=lambda item: (item[1], -item[0])
    )
    selected_holdout = metrics.filter(
        (pl.col("threshold") == selected) & (pl.col("fold") == 5)
    ).row(0, named=True)
    baseline_holdout = baseline_by_fold[5]
    selected_return = selected_holdout["mean_net_return"]
    baseline_return = baseline_holdout["mean_net_return"]
    holdout_improvement = (
        None
        if selected_return is None or baseline_return is None
        else float(selected_return) - float(baseline_return)
    )
    holdout_passed = (
        int(selected_holdout["trade_count"]) >= 5
        and selected_return is not None
        and float(selected_return) > 0
        and holdout_improvement is not None
        and holdout_improvement >= 0
    )
    return metrics, RvolChampionDecision(
        baseline=baseline,
        selected=selected if holdout_passed else baseline,
        status=(
            "research_champion_promoted" if holdout_passed else "champion_retained"
        ),
        discovery_improvement=discovery_improvement,
        holdout_improvement=holdout_improvement,
        attempted_configurations=len(challengers),
        production_eligible=False,
        reason=(
            "challenger passed four discovery folds and the untouched fifth holdout"
            if holdout_passed
            else "best discovery challenger failed the untouched fifth holdout"
        ),
    )
