"""Backtest a small, source-defined ORB family on accepted local market data."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.storage import sha256_file
from research.open_source_orb import (
    OrbConfig,
    OrbTrade,
    evaluate_orb,
    evaluate_stock_in_play_orb,
)
from scripts.build_h30_candidate_cohort import latest_gate_paths
from scripts.run_h30_challenger_backtest import _load_cohort, _rth_by_date

ROOT = Path(__file__).resolve().parents[1]
COMMISSION_PER_SHARE_ROUND_TRIP = 0.007
RISK_PER_TRADE_USD = 10_000.0
DAILY_NOTIONAL_LIMIT_USD = 2_000_000.0
MAX_TRADES_PER_DAY = 3
SOURCE_VARIANT = "source_30m_1r"
VARIANTS = {SOURCE_VARIANT: OrbConfig(target_r=1.0)} | {
    f"system_{opening}m_{str(target).replace('.', '_')}r": OrbConfig(
        opening_minutes=opening,
        target_r=target,
        max_price_stop_pct=0.015,
    )
    for opening in (15, 30, 45, 60)
    for target in (0.5, 1.0, 1.5, 2.0, 3.0)
}
PAPER_VARIANT = "paper_5m_stock_in_play_adapted"
ALL_VARIANTS = (*VARIANTS, PAPER_VARIANT)


@dataclass(frozen=True)
class Metrics:
    trades: int
    win_rate: float | None
    average_win_loss: float | None
    net_pnl: float
    profit_factor: float | None
    expectancy_per_trade: float | None
    median_pnl: float | None
    expectancy_ci95_low: float | None
    expectancy_ci95_high: float | None


def _number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    return float(value)


def _cluster_bootstrap_expectancy(
    frame: pl.DataFrame, *, iterations: int = 2_000
) -> tuple[float | None, float | None]:
    if frame.is_empty():
        return None, None
    by_day = {
        row["trade_date"]: list(row["pnls"])
        for row in frame.group_by("trade_date").agg(
            pl.col("net_pnl").alias("pnls")
        ).iter_rows(named=True)
    }
    days = list(by_day)
    if len(days) < 2:
        return None, None
    rng = random.Random(20260818)
    estimates: list[float] = []
    for _ in range(iterations):
        sample = [rng.choice(days) for _ in days]
        pnls = [pnl for day in sample for pnl in by_day[day]]
        estimates.append(sum(pnls) / len(pnls))
    estimates.sort()
    return (
        estimates[int(iterations * 0.025)],
        estimates[int(iterations * 0.975)],
    )


def summarize(frame: pl.DataFrame) -> Metrics:
    if frame.is_empty():
        return Metrics(0, None, None, 0.0, None, None, None, None, None)
    pnl = frame.get_column("net_pnl")
    wins = pnl.filter(pnl > 0)
    losses = pnl.filter(pnl < 0)
    average_win = _number(wins.mean(), "average_win") if wins.len() else None
    average_loss = abs(_number(losses.mean(), "average_loss")) if losses.len() else None
    gross_win = _number(wins.sum(), "gross_win") if wins.len() else 0.0
    gross_loss = abs(_number(losses.sum(), "gross_loss")) if losses.len() else 0.0
    ci_low, ci_high = _cluster_bootstrap_expectancy(frame)
    return Metrics(
        trades=frame.height,
        win_rate=wins.len() / frame.height,
        average_win_loss=(
            average_win / average_loss
            if average_win is not None and average_loss not in (None, 0.0)
            else None
        ),
        net_pnl=_number(pnl.sum(), "net_pnl"),
        profit_factor=gross_win / gross_loss if gross_loss else None,
        expectancy_per_trade=_number(pnl.mean(), "mean_pnl"),
        median_pnl=_number(pnl.median(), "median_pnl"),
        expectancy_ci95_low=ci_low,
        expectancy_ci95_high=ci_high,
    )


def _passes_payoff_gate(metrics: Metrics, *, minimum_trades: int) -> bool:
    return (
        metrics.trades >= minimum_trades
        and (metrics.average_win_loss or 0) > 1.2
        and (metrics.profit_factor or 0) > 1.0
        and metrics.net_pnl > 0
    )


def _validation_expectancy(result: dict[str, object]) -> float:
    validation = result.get("validation")
    if not isinstance(validation, dict):
        return 0.0
    value = validation.get("expectancy_per_trade")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _latest_counterfactual(data_root: Path) -> tuple[pl.DataFrame, Path]:
    paths = list(
        (data_root / "accepted").glob(
            "research.counterfactual_candidate_cohort-*/data.parquet"
        )
    )
    if not paths:
        fallback = list(
            (data_root / "accepted").glob(
                "research.h30_candidate_cohort-*/data.parquet"
            )
        )
        if not fallback:
            raise FileNotFoundError("candidate cohort is missing")
        path = max(fallback, key=lambda item: item.stat().st_mtime)
        return _load_cohort(data_root)[0].rename(
            {"selection_rank": "counterfactual_rank"}
        ).with_columns(
            pl.lit(True).alias("pass_gate")
        ), path
    latest = max(paths, key=lambda path: path.stat().st_mtime)
    return pl.read_parquet(latest), latest


def _atr_map(data_root: Path) -> dict[tuple[date, str], float]:
    result: dict[tuple[date, str], float] = {}
    for day, (path, _) in latest_gate_paths(data_root).items():
        frame = pl.read_parquet(path, columns=["symbol", "price", "atr_pct"])
        for row in frame.iter_rows(named=True):
            price = row["price"]
            atr_pct = row["atr_pct"]
            if isinstance(price, (int, float)) and isinstance(atr_pct, (int, float)):
                result[(day, str(row["symbol"]))] = float(price) * float(atr_pct)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", type=Path, default=ROOT / "runtime" / "ai-quant" / "data"
    )
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "runtime" / "ai-quant" / "research" / "open_source_orb.json",
    )
    args = parser.parse_args()
    cohort_raw, cohort_path = _latest_counterfactual(args.data_root)
    cohort = cohort_raw.filter(
        (pl.col("market_cap") >= 1_000_000_000) & pl.col("pass_gate")
    )
    bars_by_date, bar_parent_ids = _rth_by_date(args.data_root)
    atr_map = _atr_map(args.data_root)
    start = cohort.get_column("session_date").min()
    end = cohort.get_column("session_date").max()
    if not isinstance(start, date) or not isinstance(end, date):
        raise ValueError("candidate date range is invalid")
    schedule = {
        row["trade_date"]: row
        for row in build_xnys_schedule(start, end).iter_rows(named=True)
    }
    rows: list[dict[str, object]] = []
    for variant in ALL_VARIANTS:
        for day, group in cohort.group_by("session_date", maintain_order=True):
            trade_date = day[0]
            session = schedule.get(trade_date)
            day_bars = bars_by_date.get(trade_date)
            if session is None or day_bars is None:
                continue
            triggered: list[tuple[int, OrbTrade]] = []
            for candidate in group.iter_rows(named=True):
                symbol = str(candidate["symbol"])
                bars = day_bars.filter(pl.col("symbol") == symbol)
                if bars.is_empty():
                    continue
                if variant == PAPER_VARIANT:
                    atr_dollars = atr_map.get((trade_date, symbol))
                    if atr_dollars is None:
                        continue
                    trade = evaluate_stock_in_play_orb(
                        bars,
                        session_open_utc=session["market_open_utc"],
                        atr_dollars=atr_dollars,
                    )
                else:
                    trade = evaluate_orb(
                        bars,
                        session_open_utc=session["market_open_utc"],
                        config=VARIANTS[variant],
                    )
                if trade is not None:
                    triggered.append((int(candidate["counterfactual_rank"]), trade))
            for rank, trade in sorted(triggered, key=lambda item: item[0])[:MAX_TRADES_PER_DAY]:
                risk_per_share = max(trade.entry_px - trade.stop_level, trade.entry_px * 0.005)
                shares = min(
                    int(RISK_PER_TRADE_USD / risk_per_share),
                    int((DAILY_NOTIONAL_LIMIT_USD / MAX_TRADES_PER_DAY) / trade.entry_px),
                )
                if shares <= 0:
                    continue
                commission = max(0.70, shares * COMMISSION_PER_SHARE_ROUND_TRIP)
                rows.append(
                    {
                        "trade_date": trade_date,
                        "symbol": trade.symbol,
                        "selection_rank": rank,
                        "variant": variant,
                        "entry_ts_utc": trade.entry_ts_utc,
                        "entry_px": trade.entry_px,
                        "stop_level": trade.stop_level,
                        "target_level": trade.target_level,
                        "exit_ts_utc": trade.exit_ts_utc,
                        "exit_px": trade.exit_px,
                        "exit_reason": trade.exit_reason,
                        "shares": shares,
                        "commission_usd": commission,
                        "net_pnl": shares * (trade.exit_px - trade.entry_px) - commission,
                        "return_pct": trade.return_pct,
                        "production_eligible": False,
                    }
                )
    labels = pl.DataFrame(rows, infer_schema_length=None).sort(
        "trade_date", "variant", "selection_rank", "symbol"
    )
    dates = sorted(cohort.get_column("session_date").unique().to_list())
    validation_start = dates[max(1, int(len(dates) * 0.60))]
    reused_holdout_start = dates[max(2, int(len(dates) * 0.80))]
    results: list[dict[str, object]] = []
    for variant in ALL_VARIANTS:
        variant_rows = labels.filter(pl.col("variant") == variant)
        train_metrics = summarize(
            variant_rows.filter(pl.col("trade_date") < validation_start)
        )
        validation_metrics = summarize(
            variant_rows.filter(
                (pl.col("trade_date") >= validation_start)
                & (pl.col("trade_date") < reused_holdout_start)
            )
        )
        reused_holdout_metrics = summarize(
            variant_rows.filter(pl.col("trade_date") >= reused_holdout_start)
        )
        historical_candidate = (
            _passes_payoff_gate(train_metrics, minimum_trades=12)
            and _passes_payoff_gate(validation_metrics, minimum_trades=4)
            and _passes_payoff_gate(reused_holdout_metrics, minimum_trades=4)
        )
        full_metrics = summarize(
            labels.filter(
                pl.col("variant") == variant
            )
        )
        results.append(
            {
                "variant": variant,
                "full": asdict(full_metrics),
                "train": asdict(train_metrics),
                "validation_start": validation_start.isoformat(),
                "validation": asdict(validation_metrics),
                "reused_holdout_start": reused_holdout_start.isoformat(),
                "reused_holdout": asdict(reused_holdout_metrics),
                "meets_payoff_first_historical_gate": historical_candidate,
                "production_eligible": False,
            }
        )
    candidates = [
        row for row in results if row["meets_payoff_first_historical_gate"] is True
    ]
    selected = max(
        candidates,
        key=_validation_expectancy,
        default=None,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    trades_path = args.output.with_suffix(".trades.parquet")
    labels.write_parquet(trades_path)
    config_payload = {
        name: asdict(config) for name, config in VARIANTS.items()
    } | {PAPER_VARIANT: {"opening_minutes": 5, "atr_stop_multiple": 0.1}}
    config_sha256 = hashlib.sha256(
        json.dumps(config_payload, sort_keys=True).encode()
    ).hexdigest()
    code_sha256 = hashlib.sha256(
        (
            sha256_file(ROOT / "research" / "open_source_orb.py")
            + sha256_file(Path(__file__))
        ).encode()
    ).hexdigest()
    data_sha256 = hashlib.sha256(
        (sha256_file(cohort_path) + "".join(sorted(bar_parent_ids))).encode()
    ).hexdigest()
    args.output.write_text(
        json.dumps(
            {
                "status": "complete",
                "sources": [
                    "https://github.com/asdtroll3/ORB-Backtester",
                    "https://doi.org/10.2139/ssrn.4729284",
                ],
                "attempted_configurations": len(ALL_VARIANTS),
                "config_sha256": config_sha256,
                "code_sha256": code_sha256,
                "data_sha256": data_sha256,
                "candidate_days": cohort.height,
                "trades_path": str(trades_path),
                "results": results,
                "selection_rule": {
                    "win_rate_floor": None,
                    "average_win_loss_min": 1.2,
                    "profit_factor_min": 1.0,
                    "net_pnl_positive": True,
                    "minimum_trades": {"train": 12, "validation": 4, "reused_holdout": 4},
                },
                "selected_historical_candidate": selected,
                "limitations": [
                    "candidate cohort is catalyst/premarket gated, not the source futures universe",
                    "5-minute paper replay uses the local premarket stock-in-play "
                    "cohort; prior-14-session opening-volume ranks are unavailable",
                    "historical NBBO and market-impact costs are incomplete",
                    "the last historical segment was viewed in prior research and is not blind",
                    "a passing row is only a forward-shadow candidate, not validated alpha",
                    "all results are research-only",
                ],
                "production_eligible": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(args.output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
