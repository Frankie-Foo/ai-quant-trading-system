"""Run long-only QQQ 5-minute opening-bias replication."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import cast

import numpy as np
import polars as pl

from data_plane.calendar import build_xnys_schedule
from research.qqq_opening_bias import evaluate_qqq_opening_bias

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "research.alpaca_sip.qqq_5m_pre_rth_1m"


def _metrics(frame: pl.DataFrame) -> dict[str, object]:
    if frame.is_empty():
        return {"trades": 0}
    pnl = frame.get_column("net_pnl")
    wins = pnl.filter(pnl > 0)
    losses = pnl.filter(pnl <= 0)
    gross_loss = abs(cast(float, losses.sum()))
    values = pnl.to_numpy()
    bootstrap = np.random.default_rng(20260818).choice(
        values, size=(5_000, frame.height), replace=True
    ).mean(axis=1)
    cumulative = np.cumsum(values)
    peaks = np.maximum.accumulate(np.insert(cumulative, 0, 0))[1:]
    drawdown = cumulative - peaks
    return {
        "trades": frame.height,
        "win_rate": wins.len() / frame.height,
        "average_win_loss": cast(float, wins.mean()) / abs(cast(float, losses.mean())),
        "profit_factor": cast(float, wins.sum()) / gross_loss if gross_loss else None,
        "net_pnl": cast(float, pnl.sum()),
        "expectancy": cast(float, pnl.mean()),
        "median_pnl": cast(float, pnl.median()),
        "expectancy_95pct_ci": [
            float(np.quantile(bootstrap, 0.025)),
            float(np.quantile(bootstrap, 0.975)),
        ],
        "max_drawdown": float(drawdown.min()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", type=Path, default=ROOT / "runtime" / "ai-quant" / "data"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "runtime" / "ai-quant" / "research" / "qqq_opening_bias.json",
    )
    args = parser.parse_args()
    paths = list((args.data_root / "accepted").glob(f"{SOURCE}-*/data.parquet"))
    if not paths:
        raise FileNotFoundError("QQQ RTH dataset is missing")
    bars = pl.read_parquet(max(paths, key=lambda item: item.stat().st_mtime))
    dates = bars.with_columns(
        pl.col("ts_utc").dt.convert_time_zone("America/New_York").dt.date().alias("day")
    ).get_column("day")
    start = cast(date, dates.min())
    end = cast(date, dates.max())
    schedule = {
        row["trade_date"]: row
        for row in build_xnys_schedule(start, end).iter_rows(named=True)
    }
    daily = (
        bars.with_columns(dates.alias("day"))
        .filter(
            pl.col("ts_utc").dt.convert_time_zone("America/New_York").dt.time()
            >= pl.time(9, 30)
        )
        .group_by("day")
        .agg(pl.col("open").first(), pl.col("close").last())
        .sort("day")
        .with_columns(
            pl.col("close").shift(1).alias("previous_close"),
            pl.col("close").shift(1).rolling_mean(20).alias("sma20"),
            pl.col("close").shift(1).rolling_mean(50).alias("sma50"),
        )
    )
    daily_features = {row["day"]: row for row in daily.iter_rows(named=True)}
    rows: list[dict[str, object]] = []
    for day, frame in bars.with_columns(dates.alias("day")).group_by("day"):
        session = schedule.get(day[0])
        if session is None:
            continue
        market_open = session["market_open_utc"]
        raw = frame.drop("day")
        rth = raw.filter(pl.col("ts_utc") >= market_open)
        trade = evaluate_qqq_opening_bias(rth, session_open_utc=market_open)
        if trade is None:
            continue
        premarket = raw.filter(pl.col("ts_utc") < market_open)
        confirmed = (
            premarket.height == 5
            and float(premarket.row(-1, named=True)["close"])
            > float(premarket.row(0, named=True)["open"])
        )
        features = daily_features[day[0]]
        previous_close = cast(float | None, features["previous_close"])
        sma20 = cast(float | None, features["sma20"])
        sma50 = cast(float | None, features["sma50"])
        above_sma20 = previous_close is not None and sma20 is not None and previous_close > sma20
        trend_stack = (
            above_sma20
            and sma20 is not None
            and sma50 is not None
            and sma20 > sma50
        )
        gap_up = previous_close is not None and cast(float, features["open"]) > previous_close
        risk_per_share = trade.entry_px - trade.stop_exit_px + 0.007
        shares = min(int(1_000 / risk_per_share), int(2_000_000 / trade.entry_px))
        commission = shares * 0.007
        base = {
                "trade_date": day[0],
                "entry_time": trade.entry_time,
                "entry_px": trade.entry_px,
                "exit_time": trade.exit_time,
                "exit_px": trade.exit_px,
                "exit_reason": trade.exit_reason,
                "shares": shares,
                "net_pnl": shares * (trade.exit_px - trade.entry_px) - commission,
        }
        enabled = {
            "baseline": True,
            "qqq_0925_confirmation": confirmed,
            "above_sma20": above_sma20,
            "trend_stack": trend_stack,
            "gap_up": gap_up,
            "qqq_0925_and_trend_stack": confirmed and trend_stack,
        }
        rows.extend({"variant": name, **base} for name, keep in enabled.items() if keep)
    trades = pl.DataFrame(rows).sort("trade_date")
    variants: dict[str, object] = {}
    for (variant,), variant_trades in trades.group_by("variant"):
        yearly = {
            str(year): _metrics(frame)
            for (year,), frame in variant_trades.with_columns(
                pl.col("trade_date").dt.year().alias("year")
            ).group_by("year")
        }
        full = _metrics(variant_trades)
        average_win_loss = cast(float | None, full.get("average_win_loss"))
        profit_factor = cast(float | None, full.get("profit_factor"))
        net_pnl = cast(float, full.get("net_pnl"))
        stable_years = all(cast(float, item.get("net_pnl")) > 0 for item in yearly.values())
        variants[variant] = {
            "full": full,
            "yearly": yearly,
            "meets_payoff_first_gate": (
                (average_win_loss or 0) > 1.2
                and (profit_factor or 0) > 1.0
                and net_pnl > 0
                and stable_years
            ),
            "expectancy_ci_excludes_zero": cast(
                list[float], full["expectancy_95pct_ci"]
            )[0]
            > 0,
        }
    report = {
        "strategy": "qqq_opening_bias_long_10r_2pct_stop_realistic_slippage",
        "variants": variants,
        "selection_note": "Exploratory six-variant screen; 2026 is reused, not blind.",
        "production_eligible": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    trades.write_csv(args.output.with_suffix(".trades.csv"))
    print(args.output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
