"""Run one frozen modern H15 momentum backtest on the accepted candidate cohort."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.http import DownloadError
from data_plane.providers.alpaca import fetch_quotes
from data_plane.storage import sha256_file
from kernel.quote_costs import latest_nbbo_spread, window_nbbo_spread
from operations.local_env import load_project_env
from research.modern_momentum import (
    ModernMomentumConfig,
    ModernMomentumTrade,
    evaluate_modern_momentum,
    evaluate_modern_momentum_reentry,
)
from scripts.build_h30_candidate_cohort import latest_gate_paths
from scripts.run_h30_challenger_backtest import _rth_by_date

ROOT = Path(__file__).resolve().parents[1]
RISK_PER_TRADE_USD = 1_000.0
ATTEMPT_RISK_FRACTIONS = (0.60, 0.40)
MAX_DAILY_TRADES = 3
MAX_DAILY_NOTIONAL_USD = 2_000_000.0


def _number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    return float(value)


def _latest_cohort(data_root: Path) -> tuple[pl.DataFrame, Path]:
    paths = list(
        (data_root / "accepted").glob("research.counterfactual_candidate_cohort-*/data.parquet")
    )
    if not paths:
        raise FileNotFoundError("counterfactual candidate cohort is missing")
    path = max(paths, key=lambda item: item.stat().st_mtime)
    return pl.read_parquet(path), path


def _prior_close_map(data_root: Path) -> dict[tuple[date, str], float]:
    result: dict[tuple[date, str], float] = {}
    for day, (path, _) in latest_gate_paths(data_root).items():
        frame = pl.read_parquet(path, columns=["symbol", "price", "asof_date"])
        for row in frame.iter_rows(named=True):
            if row["asof_date"] < day and isinstance(row["price"], (int, float)):
                result[(day, str(row["symbol"]))] = float(row["price"])
    return result


def _entry_spread(symbol: str, trade: ModernMomentumTrade) -> float:
    entry_quotes = fetch_quotes(
        (symbol,),
        trade.entry_ts_utc - timedelta(seconds=30),
        trade.entry_ts_utc + timedelta(microseconds=1),
    )
    entry = latest_nbbo_spread(entry_quotes, symbol=symbol, at_utc=trade.entry_ts_utc)
    if entry is None:
        raise ValueError("entry NBBO window incomplete")
    return entry.relative_spread


def _quote_spreads(symbol: str, trade: ModernMomentumTrade) -> tuple[float, float, int]:
    entry_spread = _entry_spread(symbol, trade)
    exit_start = trade.exit_ts_utc - timedelta(minutes=1)
    exit_quotes = fetch_quotes((symbol,), exit_start, trade.exit_ts_utc + timedelta(microseconds=1))
    exit_window = window_nbbo_spread(
        exit_quotes,
        symbol=symbol,
        start_utc=exit_start,
        end_utc=trade.exit_ts_utc + timedelta(microseconds=1),
        quantile=0.95,
    )
    if exit_window is None:
        raise ValueError("NBBO window incomplete")
    return entry_spread, exit_window.relative_spread, exit_window.sample_count


def _metrics(frame: pl.DataFrame) -> dict[str, object]:
    if frame.is_empty():
        return {
            "trades": 0,
            "win_rate": None,
            "average_win_loss": None,
            "profit_factor": None,
            "net_pnl": 0.0,
            "expectancy": None,
            "expectancy_ci95": [None, None],
        }
    pnl = frame.get_column("net_pnl")
    wins = pnl.filter(pnl > 0)
    losses = pnl.filter(pnl < 0)
    gross_win = _number(wins.sum() or 0, "gross_win")
    gross_loss = abs(_number(losses.sum() or 0, "gross_loss"))
    average_win = _number(wins.mean(), "average_win") if wins.len() else None
    average_loss = abs(_number(losses.mean(), "average_loss")) if losses.len() else None
    by_day = {
        row["trade_date"]: row["pnls"]
        for row in frame.group_by("trade_date")
        .agg(pl.col("net_pnl").alias("pnls"))
        .iter_rows(named=True)
    }
    rng = random.Random(20260818)
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
        "average_win_loss": average_win / average_loss
        if average_win is not None and average_loss
        else None,
        "profit_factor": gross_win / gross_loss if gross_loss else None,
        "net_pnl": _number(pnl.sum(), "net_pnl"),
        "expectancy": _number(pnl.mean(), "expectancy"),
        "expectancy_ci95": [estimates[50], estimates[1950]]
        if estimates
        else [None, None],
    }


def _episode_metrics(frame: pl.DataFrame) -> dict[str, object]:
    if frame.is_empty():
        return _metrics(frame)
    episodes = frame.group_by("trade_date", "symbol").agg(
        pl.col("net_pnl").sum().alias("net_pnl")
    )
    return _metrics(episodes)


def _costed_trade(
    symbol: str,
    bars: pl.DataFrame,
    *,
    session_open_utc: datetime,
    first_trade: ModernMomentumTrade,
    config: ModernMomentumConfig,
) -> tuple[ModernMomentumTrade, float, float, int] | None:
    preliminary = evaluate_modern_momentum_reentry(
        bars,
        session_open_utc=session_open_utc,
        first_trade=first_trade,
        config=config,
        relative_spread=config.relative_spread,
    )
    if preliminary is None:
        return None
    entry_spread = _entry_spread(symbol, preliminary)
    trade = evaluate_modern_momentum_reentry(
        bars,
        session_open_utc=session_open_utc,
        first_trade=first_trade,
        config=config,
        relative_spread=entry_spread,
    )
    if trade is None:
        return None
    entry_spread, exit_spread, exit_samples = _quote_spreads(symbol, trade)
    return trade, entry_spread, exit_spread, exit_samples


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=ROOT / "runtime" / "ai-quant" / "data")
    parser.add_argument("--fetch-nbbo", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "runtime" / "ai-quant" / "research" / "modern_momentum.json",
    )
    args = parser.parse_args()
    load_project_env(ROOT)
    config = ModernMomentumConfig()
    cohort, cohort_path = _latest_cohort(args.data_root)
    cohort = cohort.filter(pl.col("market_cap") >= config.minimum_market_cap)
    bars_by_date, bar_ids = _rth_by_date(args.data_root)
    prior_closes = _prior_close_map(args.data_root)
    dates = sorted(cohort.get_column("session_date").unique().to_list())
    schedule = {
        row["trade_date"]: row
        for row in build_xnys_schedule(dates[0], dates[-1]).iter_rows(named=True)
    }
    rows: list[dict[str, object]] = []
    blocked: dict[str, int] = {}
    for day_group, group in cohort.group_by("session_date", maintain_order=True):
        day = day_group[0]
        session = schedule.get(day)
        day_bars = bars_by_date.get(day)
        if session is None or day_bars is None:
            blocked["bars_missing"] = blocked.get("bars_missing", 0) + group.height
            continue
        candidates: list[
            tuple[int, list[tuple[ModernMomentumTrade, float, float, int]]]
        ] = []
        for candidate in group.iter_rows(named=True):
            symbol = str(candidate["symbol"])
            prior_close = prior_closes.get((day, symbol))
            bars = day_bars.filter(pl.col("symbol") == symbol)
            if prior_close is None or bars.is_empty():
                blocked["prior_close_or_bars_missing"] = (
                    blocked.get("prior_close_or_bars_missing", 0) + 1
                )
                continue
            preliminary = evaluate_modern_momentum(
                bars,
                session_open_utc=session["market_open_utc"],
                prior_close=prior_close,
                market_cap=float(candidate["market_cap"]),
                premarket_rvol=float(candidate["rvol"]),
                config=config,
            )
            if preliminary is None:
                continue
            if not args.fetch_nbbo:
                blocked["nbbo_not_requested"] = blocked.get("nbbo_not_requested", 0) + 1
                continue
            try:
                entry_spread = _entry_spread(symbol, preliminary)
                trade = evaluate_modern_momentum(
                    bars,
                    session_open_utc=session["market_open_utc"],
                    prior_close=prior_close,
                    market_cap=float(candidate["market_cap"]),
                    premarket_rvol=float(candidate["rvol"]),
                    config=config,
                    relative_spread=entry_spread,
                )
                if trade is None:
                    continue
                entry_spread, exit_spread, exit_samples = _quote_spreads(symbol, trade)
            except (DownloadError, ValueError) as exc:
                key = f"nbbo_{type(exc).__name__}"
                blocked[key] = blocked.get(key, 0) + 1
                continue
            print(
                json.dumps(
                    {"nbbo_complete": symbol, "trade_date": day.isoformat()},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            attempts = [(trade, entry_spread, exit_spread, exit_samples)]
            if trade.exit_reason == "stop":
                try:
                    reentry = _costed_trade(
                        symbol,
                        bars,
                        session_open_utc=session["market_open_utc"],
                        first_trade=trade,
                        config=config,
                    )
                except (DownloadError, ValueError) as exc:
                    key = f"reentry_nbbo_{type(exc).__name__}"
                    blocked[key] = blocked.get(key, 0) + 1
                else:
                    if reentry is not None:
                        attempts.append(reentry)
            candidates.append((int(candidate["counterfactual_rank"]), attempts))
        selected = sorted(
            candidates, key=lambda item: (item[1][0][0].signal_ts_utc, item[0])
        )[:MAX_DAILY_TRADES]
        for rank, attempts in selected:
            for attempt_index, (
                trade,
                entry_spread,
                exit_spread,
                exit_samples,
            ) in enumerate(attempts, start=1):
                risk_fraction = ATTEMPT_RISK_FRACTIONS[attempt_index - 1]
                raw_exit = trade.exit_px / (
                    1 - entry_spread / 2 - config.market_impact_pct
                )
                exit_px = raw_exit * (
                    1 - exit_spread / 2 - config.market_impact_pct
                )
                risk_per_share = trade.entry_px * trade.all_in_stop_pct
                shares = min(
                    int(RISK_PER_TRADE_USD * risk_fraction / risk_per_share),
                    int(
                        (MAX_DAILY_NOTIONAL_USD / MAX_DAILY_TRADES)
                        / trade.entry_px
                    ),
                )
                if shares <= 0:
                    continue
                rows.append(
                    {
                        "trade_date": day,
                        "symbol": trade.symbol,
                        "selection_rank": rank,
                        "attempt": attempt_index,
                        "risk_fraction": risk_fraction,
                        "entry_ts_utc": trade.entry_ts_utc,
                        "entry_px": trade.entry_px,
                        "stop_level": trade.stop_level,
                        "target_level": trade.target_level,
                        "exit_ts_utc": trade.exit_ts_utc,
                        "exit_px": exit_px,
                        "exit_reason": trade.exit_reason,
                        "all_in_stop_pct": trade.all_in_stop_pct,
                        "entry_relative_spread": entry_spread,
                        "exit_relative_spread_p95": exit_spread,
                        "exit_quote_samples": exit_samples,
                        "modeled_market_impact_bps_per_side": (
                            config.market_impact_pct * 10_000
                        ),
                        "commission_usd": 0.0,
                        "shares": shares,
                        "net_pnl": shares * (exit_px - trade.entry_px),
                        "production_eligible": False,
                    }
                )
    labels = (
        pl.DataFrame(rows, infer_schema_length=None)
        if rows
        else pl.DataFrame(schema={"trade_date": pl.Date, "net_pnl": pl.Float64})
    )
    train_end = dates[max(1, int(len(dates) * 0.60))]
    blind_start = dates[max(2, int(len(dates) * 0.80))]
    splits = {
        "train": labels.filter(pl.col("trade_date") < train_end),
        "validation": labels.filter(
            (pl.col("trade_date") >= train_end) & (pl.col("trade_date") < blind_start)
        ),
        "blind": labels.filter(pl.col("trade_date") >= blind_start),
    }
    metrics = {name: _metrics(frame) for name, frame in splits.items()}
    metrics["full"] = _metrics(labels)
    episode_metrics = {name: _episode_metrics(frame) for name, frame in splits.items()}
    episode_metrics["full"] = _episode_metrics(labels)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    trades_path = args.output.with_suffix(".trades.parquet")
    labels.write_parquet(trades_path)
    payload = {
        "status": "complete",
        "strategy": "modern_h15_momentum_pullback_reentry_v2",
        "frozen_config": asdict(config),
        "attempted_configurations": 1,
        "blind_evaluations": 1,
        "cohort_symbol_days": cohort.height,
        "cohort_sessions": len(dates),
        "split_dates": {
            "train_end_exclusive": train_end.isoformat(),
            "blind_start": blind_start.isoformat(),
        },
        "metrics": metrics,
        "episode_metrics": episode_metrics,
        "blocked": blocked,
        "cost_model": {
            "spread": "historical Alpaca SIP NBBO; entry latest <= fill, exit-minute p95",
            "slippage": (
                "2 bps modeled market impact per side; realized slippage unavailable "
                "without historical orders"
            ),
            "commission": (
                "Alpaca US equities commission-free; pass-through regulatory fees "
                "not modeled"
            ),
        },
        "code_sha256": hashlib.sha256(
            (
                sha256_file(ROOT / "research" / "modern_momentum.py") + sha256_file(Path(__file__))
            ).encode()
        ).hexdigest(),
        "data_sha256": hashlib.sha256(
            (sha256_file(cohort_path) + "".join(sorted(bar_ids))).encode()
        ).hexdigest(),
        "trades_path": str(trades_path),
        "limitations": [
            "historical cohort is a point-in-time top-10 catalyst/premarket pool, "
            "not the full US equity universe",
            "market impact is modeled, not realized slippage",
            "regulatory sell fees are excluded",
            "one frozen configuration was evaluated; no parameter search was performed",
            "research only; production_eligible=false",
        ],
        "production_eligible": False,
    }
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
