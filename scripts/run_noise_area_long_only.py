"""Run realistic long-only Noise-Area strategy on accepted SPY SIP data."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "runtime" / "ai-quant" / "research" / "vendor" / "zarattini-2024-momentum-spy"
SOURCE = "research.alpaca_sip.spy_rth_1m"
sys.path.insert(0, str(VENDOR))


@dataclass(frozen=True)
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_px: float
    exit_px: float
    units: int
    net_pnl: float
    exit_reason: str


def run_long_only(bars: pd.DataFrame) -> pd.DataFrame:
    if not VENDOR.is_dir():
        raise FileNotFoundError(f"pinned Noise-Area vendor checkout is missing: {VENDOR}")
    from src.backtest import check_times
    from src.noise_area import _day_key, build_indicators, session_closes
    from src.sizing import daily_sigma, target_units

    indicators = build_indicators(bars, lookback=14)
    volatility = daily_sigma(session_closes(indicators), lookback=14)
    equity = 100_000.0
    position: tuple[pd.Timestamp, float, int] | None = None
    trades: list[Trade] = []
    checks = set(check_times(30))
    for day, day_bars in indicators.groupby(_day_key(indicators.index)):
        daily_vol = volatility.get(day, np.nan)
        rows = list(day_bars.iterrows())
        for index, (timestamp, row) in enumerate(rows):
            if position is not None:
                entry_time, entry_px, units = position
                if float(row["low"]) <= entry_px * 0.985:
                    exit_px = entry_px * 0.98
                    pnl = units * (exit_px - entry_px) - max(0.70, units * 0.007)
                    equity += pnl
                    trades.append(
                        Trade(
                            entry_time,
                            timestamp,
                            entry_px,
                            exit_px,
                            units,
                            pnl,
                            "2pct_all_in_stop",
                        )
                    )
                    position = None
                    continue
            if timestamp.time() not in checks:
                continue
            upper = float(row["upper"])
            if not np.isfinite(upper):
                continue
            close = float(row["close"])
            if position is not None:
                trail = max(float(row["vwap"]), upper)
                if close < trail and index + 1 < len(rows):
                    entry_time, entry_px, units = position
                    next_time, next_row = rows[index + 1]
                    exit_px = float(next_row["open"]) - 0.005
                    pnl = units * (exit_px - entry_px) - max(0.70, units * 0.007)
                    equity += pnl
                    trades.append(
                        Trade(
                            entry_time,
                            next_time,
                            entry_px,
                            exit_px,
                            units,
                            pnl,
                            "noise_vwap_trail",
                        )
                    )
                    position = None
            elif close > upper and np.isfinite(daily_vol) and index + 1 < len(rows):
                next_time, next_row = rows[index + 1]
                entry_px = float(next_row["open"]) + 0.005
                units = target_units(equity, float(daily_vol), entry_px, 0.02, 4.0)
                if units > 0:
                    position = (next_time, entry_px, units)
        if position is not None:
            entry_time, entry_px, units = position
            last_time, last = rows[-1]
            exit_px = float(last["close"]) - 0.005
            pnl = units * (exit_px - entry_px) - max(0.70, units * 0.007)
            equity += pnl
            trades.append(Trade(entry_time, last_time, entry_px, exit_px, units, pnl, "end_of_day"))
            position = None
    return pd.DataFrame([trade.__dict__ for trade in trades])


def _metrics(trades: pd.DataFrame) -> dict[str, object]:
    if trades.empty:
        return {"trades": 0}
    wins = trades.loc[trades["net_pnl"] > 0, "net_pnl"]
    losses = trades.loc[trades["net_pnl"] <= 0, "net_pnl"]
    payoff = float(wins.mean() / abs(losses.mean())) if len(wins) and len(losses) else None
    gross_loss = abs(float(losses.sum()))
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades),
        "average_win_loss": payoff,
        "profit_factor": float(wins.sum()) / gross_loss if gross_loss else None,
        "net_pnl": float(trades["net_pnl"].sum()),
        "expectancy": float(trades["net_pnl"].mean()),
        "median_pnl": float(trades["net_pnl"].median()),
    }


def _metric_number(metrics: dict[str, object], name: str) -> float:
    value = metrics.get(name)
    return float(value) if isinstance(value, (int, float)) else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", type=Path, default=ROOT / "runtime" / "ai-quant" / "data"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            ROOT
            / "runtime"
            / "ai-quant"
            / "research"
            / "noise_area_long_only.json"
        ),
    )
    args = parser.parse_args()
    paths = list((args.data_root / "accepted").glob(f"{SOURCE}-*/data.parquet"))
    if not paths:
        raise FileNotFoundError("SPY Noise-Area dataset is missing")
    path = max(paths, key=lambda item: item.stat().st_mtime)
    frame = pl.read_parquet(path).sort("ts_utc")
    values = frame.select(
        "ts_utc", "open", "high", "low", "close", "volume"
    ).to_dict(as_series=False)
    index = pd.DatetimeIndex(values.pop("ts_utc")).tz_convert("America/New_York")
    bars = pd.DataFrame(values, index=index)
    trades = run_long_only(bars)
    trades["year"] = trades["entry_time"].dt.year
    yearly = {str(year): _metrics(group) for year, group in trades.groupby("year")}
    full_metrics = _metrics(trades)
    report = {
        "strategy": "zarattini_noise_area_long_only_realistic_execution_2pct_stop",
        "source_commit": "ec10608398b86c1a48d83411ae3e0fc9ab4cbfd1",
        "full": full_metrics,
        "yearly": yearly,
        "meets_payoff_first_gate": (
            _metric_number(full_metrics, "average_win_loss") > 1.2
            and _metric_number(full_metrics, "profit_factor") > 1.0
            and _metric_number(full_metrics, "net_pnl") > 0
        ),
        "production_eligible": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    trades.drop(columns="year").to_csv(args.output.with_suffix(".trades.csv"), index=False)
    print(args.output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
