"""Backtest the precommitted nine-ETF cross-sectional momentum candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from dataclasses import asdict
from datetime import date
from pathlib import Path

import polars as pl

from data_plane.contracts import DatasetSnapshot
from data_plane.storage import sha256_file
from research.etf_cross_sectional_momentum import (
    BacktestResult,
    MomentumConfig,
    backtest,
    build_target_weights,
    month_end_indexes,
    performance,
    slice_result,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "yahoo.finance.etf_adjusted_daily"
SYMBOLS = ("SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "GLD", "VNQ", "DBC")
INITIAL_CAPITAL = 100_000.0
BASE_COST_BPS = 5.0


def _latest(data_root: Path) -> tuple[pl.DataFrame, DatasetSnapshot, Path]:
    candidates: list[tuple[DatasetSnapshot, Path]] = []
    for path in (data_root / "accepted").glob(f"{SOURCE}-*/data.parquet"):
        snapshot = DatasetSnapshot.model_validate_json(
            (path.parent / "manifest.json").read_text(encoding="utf-8")
        )
        snapshot.assert_usable()
        candidates.append((snapshot, path))
    if not candidates:
        raise FileNotFoundError("accepted adjusted ETF daily data are missing")
    snapshot, path = max(candidates, key=lambda item: item[0].asof_utc)
    return pl.read_parquet(path), snapshot, path


def _panel(frame: pl.DataFrame) -> tuple[list[date], dict[str, list[float]]]:
    rows: dict[str, dict[date, float]] = {symbol: {} for symbol in SYMBOLS}
    for row in frame.select("symbol", "ts_utc", "adjusted_close").iter_rows(named=True):
        symbol = str(row["symbol"])
        if symbol in rows:
            rows[symbol][row["ts_utc"].date()] = float(row["adjusted_close"])
    common = sorted(set.intersection(*(set(values) for values in rows.values())))
    if len(common) < 252 * 10:
        raise ValueError("common adjusted ETF history is too short")
    return common, {
        symbol: [rows[symbol][trade_date] for trade_date in common] for symbol in SYMBOLS
    }


def _fixed_targets(
    dates: list[date], selected: tuple[str, ...], weight: float
) -> list[dict[str, float]]:
    return [
        {symbol: weight if symbol in selected else 0.0 for symbol in SYMBOLS}
        for _ in dates
    ]


def _random_targets(dates: list[date], *, seed: int = 42) -> list[dict[str, float]]:
    rng = random.Random(seed)
    rebalances = month_end_indexes(dates)
    current = {symbol: 0.0 for symbol in SYMBOLS}
    output: list[dict[str, float]] = []
    for index in range(len(dates)):
        if index in rebalances and index >= 252:
            selected = set(rng.sample(SYMBOLS, 3))
            current = {
                symbol: 1 / 3 if symbol in selected else 0.0 for symbol in SYMBOLS
            }
        output.append(dict(current))
    return output


def _first_invested(result: BacktestResult) -> date:
    for trade_date, turnover in zip(result.dates, result.turnover, strict=True):
        if turnover > 0:
            return trade_date
    raise ValueError("strategy never invested")


Metric = dict[str, float | int | None]


def _periods(result: BacktestResult, start: date) -> dict[str, Metric]:
    windows = {
        "full": (start, None),
        "train": (start, date(2018, 12, 31)),
        "validation": (date(2019, 1, 1), date(2022, 12, 31)),
        "blind": (date(2023, 1, 1), None),
    }
    return {
        name: performance(slice_result(result, period_start, period_end))
        for name, (period_start, period_end) in windows.items()
    }


def _calendar_years(result: BacktestResult, start: date) -> dict[str, Metric]:
    years = sorted({value.year for value in result.dates if value >= start})
    return {
        str(year): performance(
            slice_result(result, date(year, 1, 1), date(year, 12, 31))
        )
        for year in years
    }


def _timeseries(
    result: BacktestResult, variant: str, start: date
) -> pl.DataFrame:
    rows = [
        {
            "trade_date": trade_date,
            "variant": variant,
            "daily_return": daily_return,
            "turnover": turnover,
            "equity_multiple": equity,
        }
        for trade_date, daily_return, turnover, equity in zip(
            result.dates, result.daily_returns, result.turnover, result.equity, strict=True
        )
        if trade_date >= start
    ]
    return pl.DataFrame(rows)


def _metric_float(metric: Metric, name: str) -> float:
    value = metric.get(name)
    if not isinstance(value, (int, float)):
        raise ValueError(f"metric {name!r} is unavailable")
    return float(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "runtime" / "ai-quant" / "research" / "etf_momentum.json",
    )
    args = parser.parse_args()
    frame, snapshot, data_path = _latest(args.data_root)
    dates, prices = _panel(frame)
    configs = {
        "base_top3": MomentumConfig(),
        "top1": MomentumConfig(holdings=1),
        "top5": MomentumConfig(holdings=5),
        "top3_absolute": MomentumConfig(require_positive_momentum=True),
    }
    variants: dict[str, tuple[MomentumConfig, float]] = {
        name: (config, BASE_COST_BPS) for name, config in configs.items()
    }
    variants.update(
        {
            f"base_cost_{bps}bps": (configs["base_top3"], float(bps))
            for bps in (0, 10, 20)
        }
    )
    targets = {
        name: build_target_weights(dates, prices, config)
        for name, config in configs.items()
    }
    runs: dict[str, BacktestResult] = {}
    for name, (config, cost_bps) in variants.items():
        target_name = next(key for key, value in configs.items() if value == config)
        runs[name] = backtest(dates, prices, targets[target_name], cost_bps=cost_bps)
    benchmarks = {
        "spy_buy_hold": backtest(
            dates, prices, _fixed_targets(dates, ("SPY",), 1.0), cost_bps=BASE_COST_BPS
        ),
        "equal_weight_9": backtest(
            dates,
            prices,
            _fixed_targets(dates, SYMBOLS, 1 / len(SYMBOLS)),
            cost_bps=BASE_COST_BPS,
        ),
        "random_top3": backtest(
            dates, prices, _random_targets(dates), cost_bps=BASE_COST_BPS
        ),
    }
    start = _first_invested(runs["base_top3"])
    results = {name: _periods(result, start) for name, result in runs.items()}
    benchmark_results = {
        name: _periods(result, start) for name, result in benchmarks.items()
    }
    years = _calendar_years(runs["base_top3"], start)
    positive_years = sum(
        _metric_float(item, "total_return") > 0 for item in years.values()
    )
    full = results["base_top3"]["full"]
    blind = results["base_top3"]["blind"]
    passes_screen = (
        _metric_float(full, "daily_profit_factor") > 1.05
        and _metric_float(blind, "total_return") > 0
        and positive_years > len(years) / 2
    )
    configurations = {
        name: {"config": asdict(config), "cost_bps": cost_bps}
        for name, (config, cost_bps) in variants.items()
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    timeseries_path = args.output.with_suffix(".timeseries.parquet")
    pl.concat(
        [
            _timeseries(result, name, start)
            for name, result in {**runs, **benchmarks}.items()
        ]
    ).write_parquet(timeseries_path)
    payload = {
        "status": "complete",
        "hypothesis": (
            "Nine-ETF 12-minus-1-month cross-sectional momentum remains profitable "
            "after T+1 execution and 5 bps per unit of turnover."
        ),
        "source_strategy": "monishkk/quant-research-platform",
        "date_range": [start.isoformat(), dates[-1].isoformat()],
        "symbols": SYMBOLS,
        "attempted_configurations": len(variants),
        "configurations": configurations,
        "config_sha256": hashlib.sha256(
            json.dumps(configurations, sort_keys=True).encode()
        ).hexdigest(),
        "execution": "signal at month-end close; target applied to next close-to-close return",
        "cost_model": {
            "base_bps_per_unit_turnover": BASE_COST_BPS,
            "components": {"commission_bps": 2, "slippage_bps": 3},
            "initial_entry_charged": True,
        },
        "split_dates": {
            "train_end": "2018-12-31",
            "validation": "2019-01-01..2022-12-31",
            "blind_start": "2023-01-01",
        },
        "dataset_id": snapshot.dataset_id,
        "data_sha256": snapshot.content_sha256,
        "code_sha256": hashlib.sha256(
            (
                sha256_file(ROOT / "research" / "etf_cross_sectional_momentum.py")
                + sha256_file(Path(__file__))
            ).encode()
        ).hexdigest(),
        "results": results,
        "benchmarks": benchmark_results,
        "calendar_years_base": years,
        "screening": {
            "profit_factor_over_1_05": _metric_float(full, "daily_profit_factor") > 1.05,
            "blind_net_positive": _metric_float(blind, "total_return") > 0,
            "majority_years_positive": positive_years > len(years) / 2,
            "positive_years": positive_years,
            "years": len(years),
            "decision": "continue_research" if passes_screen else "reject",
        },
        "timeseries_path": str(timeseries_path),
        "source_data_path": str(data_path),
        "limitations": [
            "Yahoo adjusted close is suitable for research but not an executable quote source.",
            "Static linear costs omit market impact, taxes, and weight drift.",
            "The fixed ETF universe has survivorship bias.",
            "Sensitivity variants cannot replace the precommitted base hypothesis.",
            "No Paper deployment is authorized by this research run.",
        ],
        "production_eligible": False,
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "screening": payload["screening"]}))


if __name__ == "__main__":
    main()
