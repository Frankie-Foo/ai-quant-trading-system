"""Backtest the user-supplied long-only ETF ORB and VWAP pullback draft."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DatasetSnapshot
from data_plane.storage import sha256_file
from research.etf_intraday_draft import DraftConfig, DraftTrade, FiveBar, ema, find_trades
from research.registry import (
    ExperimentEvidence,
    PerformanceEvidence,
    ScientificHypothesis,
    evaluate_experiment,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "alpaca.sip.etf_rth_1m"
SYMBOLS = ("SPY", "QQQ", "IWM")
INITIAL_EQUITY = 1_000_000.0
COMMISSION_PER_SHARE_ROUND_TRIP = 0.007


def _number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    return float(value)


def _load(data_root: Path) -> tuple[pl.DataFrame, tuple[str, ...]]:
    frames: list[pl.DataFrame] = []
    dataset_ids: list[str] = []
    for path in sorted((data_root / "accepted").glob(f"{SOURCE}-*/data.parquet")):
        snapshot = DatasetSnapshot.model_validate_json(
            (path.parent / "manifest.json").read_text(encoding="utf-8")
        )
        snapshot.assert_usable()
        frames.append(pl.read_parquet(path))
        dataset_ids.append(snapshot.dataset_id)
    if not frames:
        raise FileNotFoundError("accepted ETF RTH bars are missing")
    return (
        pl.concat(frames, how="diagonal_relaxed")
        .unique(("symbol", "ts_utc"), keep="last")
        .sort("symbol", "ts_utc"),
        tuple(dataset_ids),
    )


def _five_by_day(
    bars: pl.DataFrame,
) -> tuple[dict[date, dict[str, list[FiveBar]]], list[date]]:
    dated = bars.with_columns(
        pl.col("ts_utc")
        .dt.convert_time_zone("America/New_York")
        .dt.date()
        .alias("trade_date")
    )
    dates = sorted(dated["trade_date"].unique().to_list())
    schedule = {
        row["trade_date"]: row
        for row in build_xnys_schedule(dates[0], dates[-1]).iter_rows(named=True)
    }
    partitions = dated.partition_by(("trade_date", "symbol"), as_dict=True)
    history: dict[str, list[float]] = {symbol: [] for symbol in SYMBOLS}
    output: dict[date, dict[str, list[FiveBar]]] = {}
    for trade_date in dates:
        session = schedule.get(trade_date)
        if session is None:
            continue
        day: dict[str, list[FiveBar]] = {}
        for symbol in SYMBOLS:
            minute = partitions.get((trade_date, symbol))
            if minute is None:
                continue
            minute = minute.drop("trade_date")
            raw = _aggregate_five(minute, session["market_open_utc"])
            if not raw:
                continue
            closes = [bar[4] for bar in raw]
            seed = history[symbol]
            ema9 = ema(closes, 9, seed)
            ema20 = ema(closes, 20, seed)
            ema50 = ema(closes, 50, seed)
            day[symbol] = [
                FiveBar(
                    ts_utc=bar[0],
                    open=bar[1],
                    high=bar[2],
                    low=bar[3],
                    close=bar[4],
                    volume=bar[5],
                    vwap=bar[6],
                    ema9=ema9[index],
                    ema20=ema20[index],
                    ema50=ema50[index],
                )
                for index, bar in enumerate(raw)
            ]
            history[symbol] = [*seed, *closes][-100:]
        if set(day) == set(SYMBOLS):
            output[trade_date] = day
    return output, sorted(output)


def _aggregate_five(
    minute: pl.DataFrame, session_open_utc: datetime
) -> list[tuple[datetime, float, float, float, float, float, float]]:
    rows = minute.sort("ts_utc").iter_rows(named=True)
    values = list(rows)
    output: list[tuple[datetime, float, float, float, float, float, float]] = []
    cumulative_value = 0.0
    cumulative_volume = 0.0
    for offset in range(0, len(values) - 4, 5):
        chunk = values[offset : offset + 5]
        start = session_open_utc + timedelta(minutes=offset)
        if [row["ts_utc"] for row in chunk] != [
            start + timedelta(minutes=index) for index in range(5)
        ]:
            break
        volume = sum(float(row["volume"]) for row in chunk)
        if volume <= 0:
            break
        cumulative_value += sum(
            float(row["vwap"]) * float(row["volume"]) for row in chunk
        )
        cumulative_volume += volume
        output.append(
            (
                start,
                float(chunk[0]["open"]),
                max(float(row["high"]) for row in chunk),
                min(float(row["low"]) for row in chunk),
                float(chunk[-1]["close"]),
                volume,
                cumulative_value / cumulative_volume,
            )
        )
    return output


def _portfolio(
    candidates: dict[date, list[DraftTrade]],
) -> pl.DataFrame:
    equity = INITIAL_EQUITY
    rows: list[dict[str, object]] = []
    for trade_date, trades in sorted(candidates.items()):
        day_start = equity
        day_pnl = 0.0
        last_exit = None
        for trade in sorted(trades, key=lambda item: (item.entry_ts_utc, item.symbol)):
            if last_exit is not None and trade.entry_ts_utc < last_exit:
                continue
            if day_pnl <= -0.038 * day_start:
                break
            all_in_risk = (
                trade.risk_per_share
                + trade.stop_level * 0.0008
                + COMMISSION_PER_SHARE_ROUND_TRIP
            )
            shares = min(
                int(equity * 0.019 / all_in_risk),
                int(equity * 0.25 / trade.entry_px),
                int(trade.entry_bar_volume * 0.01),
            )
            if shares <= 0:
                continue
            pnl = shares * (trade.gross_exit_value_per_share - trade.entry_px)
            pnl -= shares * COMMISSION_PER_SHARE_ROUND_TRIP
            equity += pnl
            day_pnl += pnl
            last_exit = trade.exit_ts_utc
            rows.append(
                {
                    "trade_date": trade_date,
                    **asdict(trade),
                    "shares": shares,
                    "net_pnl": pnl,
                    "equity": equity,
                }
            )
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


def _metrics(frame: pl.DataFrame) -> dict[str, object]:
    if frame.is_empty():
        return {"trades": 0, "net_pnl": 0.0}
    pnl = frame["net_pnl"]
    wins = pnl.filter(pnl > 0)
    losses = pnl.filter(pnl < 0)
    gross_win = _number(wins.sum() or 0, "gross win")
    gross_loss = abs(_number(losses.sum() or 0, "gross loss"))
    average_win = _number(wins.mean(), "average win") if wins.len() else None
    average_loss = abs(_number(losses.mean(), "average loss")) if losses.len() else None
    outcomes = [float(value) for value in pnl]
    consecutive = maximum = 0
    for value in outcomes:
        consecutive = consecutive + 1 if value < 0 else 0
        maximum = max(maximum, consecutive)
    equity = frame["equity"]
    peaks = equity.cum_max()
    drawdown = equity / peaks - 1
    by_day = {
        row["trade_date"]: row["pnls"]
        for row in frame.group_by("trade_date")
        .agg(pl.col("net_pnl").alias("pnls"))
        .iter_rows(named=True)
    }
    rng = random.Random(20260819)
    estimates: list[float] = []
    days = list(by_day)
    if len(days) > 1:
        for _ in range(2_000):
            sample = [rng.choice(days) for _ in days]
            values = [value for day in sample for value in by_day[day]]
            estimates.append(sum(values) / len(values))
        estimates.sort()
    return {
        "trades": frame.height,
        "win_rate": wins.len() / frame.height,
        "average_win_loss": (
            average_win / average_loss if average_win is not None and average_loss else None
        ),
        "profit_factor": gross_win / gross_loss if gross_loss else None,
        "net_pnl": _number(pnl.sum(), "net pnl"),
        "expectancy": _number(pnl.mean(), "expectancy"),
        "expectancy_ci95": [estimates[50], estimates[1950]] if estimates else [None, None],
        "max_drawdown": _number(drawdown.min(), "maximum drawdown"),
        "max_consecutive_losses": maximum,
    }


def _performance(metric: dict[str, object]) -> PerformanceEvidence:
    interval = metric.get("expectancy_ci95")
    if not isinstance(interval, list) or len(interval) != 2:
        raise ValueError("expectancy confidence interval is unavailable")

    def optional(value: object) -> float | None:
        return None if value is None else _number(value, "performance metric")

    trades = metric.get("trades")
    if not isinstance(trades, int):
        raise ValueError("trade count is unavailable")
    return PerformanceEvidence(
        trades=trades,
        win_rate=optional(metric.get("win_rate")),
        average_win_loss=optional(metric.get("average_win_loss")),
        profit_factor=optional(metric.get("profit_factor")),
        expectancy=optional(metric.get("expectancy")),
        expectancy_ci95=(optional(interval[0]), optional(interval[1])),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", type=Path, default=ROOT / "runtime" / "ai-quant" / "data"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "runtime" / "ai-quant" / "research" / "etf_intraday_draft.json",
    )
    args = parser.parse_args()
    bars, dataset_ids = _load(args.data_root)
    days, dates = _five_by_day(bars)
    variants = {
        "combined_base": (DraftConfig(), True, True),
        **{
            f"orb_volume_{value:.1f}": (
                DraftConfig(orb_volume_multiple=value), True, False
            )
            for value in (1.3, 1.5, 1.7)
        },
        **{
            f"pullback_volume_{value:.1f}": (
                DraftConfig(pullback_volume_multiple=value), False, True
            )
            for value in (0.9, 1.0, 1.1)
        },
        **{
            f"combined_cost_{bps}bps": (
                DraftConfig(cost_pct_per_side=bps / 10_000), True, True
            )
            for bps in (1, 2, 4)
        },
        "combined_friction_floor": (
            DraftConfig(cost_pct_per_side=0.0, stop_slippage_pct=0.0), True, True
        ),
    }
    train_end = dates[int(len(dates) * 0.60)]
    blind_start = dates[int(len(dates) * 0.80)]
    results: dict[str, object] = {}
    trade_frames: list[pl.DataFrame] = []
    for name, (config, allow_orb, allow_pullback) in variants.items():
        candidates: dict[date, list[DraftTrade]] = {}
        for trade_date, by_symbol in days.items():
            for symbol in SYMBOLS:
                benchmark_symbol = "QQQ" if symbol == "SPY" else "SPY"
                candidates.setdefault(trade_date, []).extend(
                    find_trades(
                        symbol,
                        by_symbol[symbol],
                        by_symbol[benchmark_symbol],
                        session_open_utc=by_symbol[symbol][0].ts_utc,
                        config=config,
                        allow_orb=allow_orb,
                        allow_pullback=allow_pullback,
                    )
                )
        trades = _portfolio(candidates)
        if not trades.is_empty():
            trade_frames.append(trades.with_columns(pl.lit(name).alias("variant")))
        results[name] = {
            "full": _metrics(trades),
            "train": _metrics(trades.filter(pl.col("trade_date") < train_end))
            if not trades.is_empty() else _metrics(trades),
            "validation": _metrics(
                trades.filter(
                    (pl.col("trade_date") >= train_end)
                    & (pl.col("trade_date") < blind_start)
                )
            ) if not trades.is_empty() else _metrics(trades),
            "blind": _metrics(trades.filter(pl.col("trade_date") >= blind_start))
            if not trades.is_empty() else _metrics(trades),
        }
    base = cast(dict[str, dict[str, object]], results["combined_base"])
    hypothesis = ScientificHypothesis(
        hypothesis_id="etf-orb-vwap-pullback.v1",
        statement="The combined ETF ORB and VWAP pullback rules have positive net expectancy.",
        mechanism=(
            "Breakout volume and trend pullback confirmation should identify persistent "
            "intraday demand."
        ),
        falsification=(
            "Reject when chronological blind expectancy is not positive after implementable costs."
        ),
        changed_variable="entry_and_exit_rule_family",
        control="no_trade_zero_return",
        validation_plan=(
            "Use chronological train, validation, and blind windows plus volume and cost "
            "sensitivity before Paper testing."
        ),
        evidence_ids=dataset_ids,
    )
    scientific_evidence = ExperimentEvidence(
        hypothesis=hypothesis,
        full=_performance(base["full"]),
        blind=_performance(base["blind"]),
        attempted_configurations=len(variants),
        blind_evaluations=1,
        point_in_time=True,
        quote_aware_costs=False,
        critical_quality_passed=True,
    )
    scientific_decision = evaluate_experiment(scientific_evidence)
    variant_configs = {
        name: {
            "config": asdict(config),
            "allow_orb": allow_orb,
            "allow_pullback": allow_pullback,
        }
        for name, (config, allow_orb, allow_pullback) in variants.items()
    }
    all_trades = pl.concat(trade_frames, how="diagonal_relaxed")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    trades_path = args.output.with_suffix(".trades.parquet")
    all_trades.write_parquet(trades_path)
    args.output.write_text(
        json.dumps(
            {
                "status": "complete",
                "source_document": "美股日内只做多量化交易策略.md",
                "date_range": [dates[0].isoformat(), dates[-1].isoformat()],
                "sessions": len(dates),
                "symbols": SYMBOLS,
                "attempted_configurations": len(variants),
                "variant_configs": variant_configs,
                "config_sha256": hashlib.sha256(
                    json.dumps(variant_configs, sort_keys=True).encode()
                ).hexdigest(),
                "split_dates": {
                    "train_end_exclusive": train_end.isoformat(),
                    "blind_start": blind_start.isoformat(),
                },
                "frozen_interpretations": {
                    "volume_history": "same-session completed five-minute bars only",
                    "near_vwap_or_ema9": "bar intersects a 0.20 percent band",
                    "rising_ema": "ema20 and ema50 exceed their value three bars earlier",
                    "benchmark": "SPY for QQQ/IWM; QQQ for SPY; close must be above VWAP",
                    "same_bar_ambiguity": "stop first",
                    "portfolio": "one position at a time; one percent volume participation",
                },
                "cost_model": {
                    "spread_and_impact_per_side_bps": 3.0,
                    "stop_extra_slippage_bps": 5.0,
                    "commission_per_share_round_trip": COMMISSION_PER_SHARE_ROUND_TRIP,
                },
                "risk_model": {
                    "initial_equity": INITIAL_EQUITY,
                    "risk_budget_pct": 0.019,
                    "single_position_notional_cap_pct": 0.25,
                    "daily_loss_stop_pct": 0.038,
                    "maximum_volume_participation_pct": 0.01,
                    "maximum_concurrent_positions": 1,
                },
                "dataset_ids": dataset_ids,
                "data_sha256": hashlib.sha256(
                    "".join(sorted(dataset_ids)).encode()
                ).hexdigest(),
                "code_sha256": hashlib.sha256(
                    (
                        sha256_file(ROOT / "research" / "etf_intraday_draft.py")
                        + sha256_file(Path(__file__))
                    ).encode()
                ).hexdigest(),
                "results": results,
                "scientific_method": {
                    "hypothesis": hypothesis.model_dump(mode="json"),
                    "evidence": scientific_evidence.model_dump(mode="json"),
                    "decision": scientific_decision.model_dump(mode="json"),
                    "screening_decision": "rejected_negative_net_expectancy",
                },
                "trades_path": str(trades_path),
                "limitations": [
                    "fixed conservative spread is a screening cost, not historical NBBO",
                    "partial fills use a one percent five-minute volume participation cap",
                    "perturbation variants are diagnostics and may not select from blind results",
                    "Paper validation remains required",
                ],
                "production_eligible": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(args.output)


if __name__ == "__main__":
    main()
