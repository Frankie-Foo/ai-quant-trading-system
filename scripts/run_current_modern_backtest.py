"""Run the current-lock three-year modern H15 backtest with full costs."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.http import DownloadError
from operations.local_env import load_project_env
from research.modern_momentum import (
    ModernMomentumConfig,
    ModernMomentumTrade,
    evaluate_modern_momentum,
)
from research.registry import (
    ExperimentEvidence,
    PerformanceEvidence,
    ScientificHypothesis,
    evaluate_experiment,
)
from scripts.run_h30_challenger_backtest import _rth_by_date
from scripts.run_modern_momentum_backtest import (
    ATTEMPT_RISK_FRACTIONS,
    MAX_DAILY_NOTIONAL_USD,
    MAX_DAILY_TRADES,
    RISK_PER_TRADE_USD,
    _costed_trade,
    _entry_spread,
    _episode_metrics,
    _metrics,
    _quote_spreads,
)

ROOT = Path(__file__).resolve().parents[1]
MIN_MARKET_CAP = 1_000_000_000.0
NBBO_WORKERS = 4


def _performance_evidence(metric: dict[str, object]) -> PerformanceEvidence:
    def optional_float(value: object) -> float | None:
        if value is None:
            return None
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError("performance metric must be numeric or null")
        return float(value)

    trades = metric["trades"]
    if not isinstance(trades, int) or isinstance(trades, bool):
        raise TypeError("trades must be an integer")
    interval = metric["expectancy_ci95"]
    if not isinstance(interval, list) or len(interval) != 2:
        raise ValueError("expectancy_ci95 must contain two values")
    return PerformanceEvidence(
        trades=trades,
        win_rate=optional_float(metric["win_rate"]),
        average_win_loss=optional_float(metric["average_win_loss"]),
        profit_factor=optional_float(metric["profit_factor"]),
        expectancy=optional_float(metric["expectancy"]),
        expectancy_ci95=(optional_float(interval[0]), optional_float(interval[1])),
    )


def _market_caps(data_root: Path) -> dict[tuple[date, str], float]:
    result: dict[tuple[date, str], float] = {}
    patterns = (
        "massive.ticker_details-*/data.parquet",
        "massive.ticker_details.current_modern_signals-*/data.parquet",
        "sec.companyfacts.derived_market_cap-*/data.parquet",
    )
    for pattern in patterns:
        for path in (data_root / "accepted").glob(pattern):
            frame = pl.read_parquet(path)
            if not {"asof_date", "symbol", "market_cap"}.issubset(frame.columns):
                continue
            for row in frame.iter_rows(named=True):
                value = row["market_cap"]
                if isinstance(value, (int, float)) and value > 0:
                    result[(row["asof_date"], str(row["symbol"]))] = float(value)
    return result


def _cost_candidate(
    signal: dict[str, Any],
    market_cap: float,
    session_open_utc: datetime,
    bars: pl.DataFrame,
    config: ModernMomentumConfig,
) -> tuple[
    str | None,
    tuple[date, int, str, list[tuple[ModernMomentumTrade, float, float, int]]] | None,
]:
    target = signal["session_date"]
    symbol = str(signal["symbol"])
    preliminary = evaluate_modern_momentum(
        bars,
        session_open_utc=session_open_utc,
        prior_close=float(signal["prior_close"]),
        market_cap=market_cap,
        premarket_rvol=float(signal["premarket_rvol"]),
        config=config,
    )
    if preliminary is None:
        return "preliminary_invalidated", None
    try:
        entry_spread = _entry_spread(symbol, preliminary)
        trade = evaluate_modern_momentum(
            bars,
            session_open_utc=session_open_utc,
            prior_close=float(signal["prior_close"]),
            market_cap=market_cap,
            premarket_rvol=float(signal["premarket_rvol"]),
            config=config,
            relative_spread=entry_spread,
        )
        if trade is None:
            return "spread_invalidated_signal", None
        entry_spread, exit_spread, exit_samples = _quote_spreads(symbol, trade)
        attempts = [(trade, entry_spread, exit_spread, exit_samples)]
        if trade.exit_reason == "stop":
            reentry = _costed_trade(
                symbol,
                bars,
                session_open_utc=session_open_utc,
                first_trade=trade,
                config=config,
            )
            if reentry is not None:
                attempts.append(reentry)
    except (DownloadError, ValueError) as exc:
        return f"nbbo_{type(exc).__name__}", None
    return None, (target, int(signal["selection_rank"]), symbol, attempts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signals", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    load_project_env(ROOT)
    signals = pl.read_parquet(args.signals)
    bars_by_date, _ = _rth_by_date(args.data_root)
    dates = sorted(signals.get_column("session_date").unique().to_list())
    schedule_frame = build_xnys_schedule(dates[0] - timedelta(days=10), dates[-1])
    schedule = {
        row["trade_date"]: row for row in schedule_frame.iter_rows(named=True)
    }
    all_sessions = schedule_frame.get_column("trade_date").to_list()
    previous = {
        target: max(item for item in all_sessions if item < target) for target in dates
    }
    caps = _market_caps(args.data_root)
    missing = {
        (previous[row["session_date"]], str(row["symbol"]))
        for row in signals.select("session_date", "symbol").iter_rows(named=True)
        if (previous[row["session_date"]], str(row["symbol"])) not in caps
    }

    config = ModernMomentumConfig()
    candidates: dict[
        date, list[tuple[int, list[tuple[ModernMomentumTrade, float, float, int]]]]
    ] = {}
    blocked: dict[str, int] = {}
    work: list[tuple[dict[str, Any], float, datetime, pl.DataFrame]] = []
    for signal in signals.iter_rows(named=True):
        target = signal["session_date"]
        symbol = str(signal["symbol"])
        cap_key = (previous[target], symbol)
        if cap_key in missing:
            blocked["market_cap_unavailable"] = blocked.get("market_cap_unavailable", 0) + 1
            continue
        market_cap = caps[cap_key]
        if market_cap < MIN_MARKET_CAP:
            blocked["market_cap_below_1b"] = blocked.get("market_cap_below_1b", 0) + 1
            continue
        session = schedule[target]
        bars = bars_by_date[target].filter(pl.col("symbol") == symbol)
        work.append((signal, market_cap, session["market_open_utc"], bars))

    with ThreadPoolExecutor(max_workers=NBBO_WORKERS) as executor:
        futures = [
            executor.submit(_cost_candidate, signal, market_cap, opened, bars, config)
            for signal, market_cap, opened, bars in work
        ]
        for future in as_completed(futures):
            reason, result = future.result()
            if reason is not None:
                blocked[reason] = blocked.get(reason, 0) + 1
                continue
            assert result is not None
            target, rank, symbol, attempts = result
            candidates.setdefault(target, []).append((rank, attempts))
            print(
                json.dumps({"event": "cost_complete", "date": str(target), "symbol": symbol}),
                flush=True,
            )

    rows: list[dict[str, object]] = []
    for target, day_candidates in sorted(candidates.items()):
        selected = sorted(
            day_candidates,
            key=lambda item: (item[1][0][0].signal_ts_utc, item[0]),
        )[:MAX_DAILY_TRADES]
        for rank, attempts in selected:
            for attempt_index, packed in enumerate(attempts, start=1):
                trade, entry_spread, exit_spread, exit_samples = packed
                risk_fraction = ATTEMPT_RISK_FRACTIONS[attempt_index - 1]
                raw_exit = trade.exit_px / (
                    1 - entry_spread / 2 - config.market_impact_pct
                )
                exit_px = raw_exit * (1 - exit_spread / 2 - config.market_impact_pct)
                risk_per_share = trade.entry_px * trade.all_in_stop_pct
                shares = min(
                    int(RISK_PER_TRADE_USD * risk_fraction / risk_per_share),
                    int((MAX_DAILY_NOTIONAL_USD / MAX_DAILY_TRADES) / trade.entry_px),
                )
                if shares <= 0:
                    continue
                values = asdict(trade)
                rows.append(
                    {
                        "trade_date": target,
                        "selection_rank": rank,
                        "attempt": attempt_index,
                        "risk_fraction": risk_fraction,
                        **values,
                        "exit_px": exit_px,
                        "entry_relative_spread": entry_spread,
                        "exit_relative_spread_p95": exit_spread,
                        "exit_quote_samples": exit_samples,
                        "shares": shares,
                        "net_pnl": shares * (exit_px - trade.entry_px),
                    }
                )
    labels = (
        pl.DataFrame(rows, infer_schema_length=None)
        if rows
        else pl.DataFrame(
            schema={"trade_date": pl.Date, "symbol": pl.String, "net_pnl": pl.Float64}
        )
    )
    trade_dates = sorted(signals.get_column("session_date").unique().to_list())
    train_end = trade_dates[int(len(trade_dates) * 0.60)]
    blind_start = trade_dates[int(len(trade_dates) * 0.80)]
    splits = {
        "train": labels.filter(pl.col("trade_date") < train_end),
        "validation": labels.filter(
            (pl.col("trade_date") >= train_end) & (pl.col("trade_date") < blind_start)
        ),
        "blind": labels.filter(pl.col("trade_date") >= blind_start),
    }
    metrics = {name: _metrics(frame) for name, frame in splits.items()}
    metrics["full"] = _metrics(labels)
    episodes = {name: _episode_metrics(frame) for name, frame in splits.items()}
    episodes["full"] = _episode_metrics(labels)
    hypothesis = ScientificHypothesis(
        hypothesis_id="modern-h15-spread-guard.v3",
        statement="A ten-basis-point entry-spread cap improves H15 net expectancy.",
        mechanism=(
            "Rejecting expensive entries should preserve gross momentum edge from trading friction."
        ),
        falsification=(
            "Reject when chronological blind episode expectancy is not positive after full costs."
        ),
        changed_variable="maximum_entry_relative_spread_0.001",
        control="modern-h15-breakout-reentry.v2",
        validation_plan=(
            "Use chronological train, validation, and one blind split with point-in-time "
            "features and historical quote-aware costs."
        ),
        evidence_ids=("current_modern_h15_spread_guard",),
    )
    scientific_evidence = ExperimentEvidence(
        hypothesis=hypothesis,
        full=_performance_evidence(episodes["full"]),
        blind=_performance_evidence(episodes["blind"]),
        attempted_configurations=1,
        blind_evaluations=1,
        point_in_time=True,
        quote_aware_costs=True,
        critical_quality_passed=True,
    )
    scientific_decision = evaluate_experiment(scientific_evidence)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    trades_path = args.output.with_suffix(".trades.parquet")
    labels.write_parquet(trades_path)
    payload = {
        "status": "complete",
        "strategy": "current_lock_modern_h15_spread_guard_v3",
        "frozen_config": asdict(config),
        "signals_before_market_cap": signals.height,
        "market_cap_unavailable": [
            {"asof_date": asof_date.isoformat(), "symbol": symbol}
            for asof_date, symbol in sorted(missing)
        ],
        "metrics": metrics,
        "episode_metrics": episodes,
        "blocked": blocked,
        "split_dates": {
            "train_end_exclusive": train_end.isoformat(),
            "blind_start": blind_start.isoformat(),
        },
        "cost_model": {
            "spread": "historical Alpaca SIP NBBO",
            "impact_bps_per_side": config.market_impact_pct * 10_000,
            "commission": 0.0,
        },
        "scientific_method": {
            "hypothesis": hypothesis.model_dump(mode="json"),
            "evidence": scientific_evidence.model_dump(mode="json"),
            "decision": scientific_decision.model_dump(mode="json"),
        },
        "trades_path": str(trades_path),
        "production_eligible": False,
    }
    args.output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps(payload, default=str))


if __name__ == "__main__":
    main()
