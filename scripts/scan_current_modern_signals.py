"""Scan preliminary H15 signals before the slow point-in-time market-cap gate."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

import polars as pl

from data_plane.calendar import build_xnys_schedule
from operations.local_env import project_data_root
from research.modern_momentum import ModernMomentumConfig, evaluate_modern_momentum
from scripts.run_h30_challenger_backtest import _rth_by_date

ROOT = Path(__file__).resolve().parents[1]
COHORT_SOURCE = "research.current_event_rvol_cohort"


def _latest_cohort(data_root: Path) -> pl.DataFrame:
    paths = list((data_root / "accepted").glob(f"{COHORT_SOURCE}-*/data.parquet"))
    if not paths:
        raise FileNotFoundError("current event RVOL cohort is missing")
    path = max(paths, key=lambda item: item.stat().st_mtime)
    return pl.read_parquet(path)


def scan_signals(cohort: pl.DataFrame, bars_by_date: dict[date, pl.DataFrame]) -> pl.DataFrame:
    dates = sorted(cohort.get_column("session_date").unique().to_list())
    schedule = {
        row["trade_date"]: row
        for row in build_xnys_schedule(dates[0], dates[-1]).iter_rows(named=True)
    }
    config = ModernMomentumConfig()
    rows: list[dict[str, object]] = []
    for candidate in cohort.iter_rows(named=True):
        target = candidate["session_date"]
        session = schedule.get(target)
        daily_bars = bars_by_date.get(target)
        if session is None or daily_bars is None:
            continue
        symbol = str(candidate["symbol"])
        trade = evaluate_modern_momentum(
            daily_bars.filter(pl.col("symbol") == symbol),
            session_open_utc=session["market_open_utc"],
            prior_close=float(candidate["prior_close"]),
            market_cap=float("inf"),
            premarket_rvol=float(candidate["premarket_rvol"]),
            config=config,
        )
        if trade is None:
            continue
        rows.append(
            {
                "session_date": target,
                "selection_rank": int(candidate["selection_rank"]),
                "prior_close": float(candidate["prior_close"]),
                "premarket_rvol": float(candidate["premarket_rvol"]),
                **asdict(trade),
            }
        )
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "runtime" / "ai-quant" / "research" / "current_modern_signals.parquet",
    )
    args = parser.parse_args()
    bars_by_date, _ = _rth_by_date(args.data_root)
    signals = scan_signals(_latest_cohort(args.data_root), bars_by_date)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    signals.write_parquet(args.output)
    print(
        json.dumps(
            {
                "status": "complete",
                "signals": signals.height,
                "sessions": signals["session_date"].n_unique() if signals.height else 0,
                "symbols": signals["symbol"].n_unique() if signals.height else 0,
                "output": str(args.output),
                "market_cap_gate_applied": False,
                "production_eligible": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
