"""Run the current-lock three-year modern H15 backtest with full costs."""

from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from data_plane.calendar import build_xnys_schedule
from data_plane.http import DownloadError
from data_plane.storage import sha256_file
from operations.local_env import load_project_env
from research.modern_momentum import (
    ModernMomentumConfig,
    ModernMomentumTrade,
    evaluate_modern_momentum,
    modern_strategy_manifest,
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


class ModernExperimentMetadata(BaseModel):
    """Operator-supplied cumulative counts, including this run; never inferred as one.

    This cohort has already been inspected. Blind evaluations count historical
    evaluations only; this replay is another holdout evaluation, not a new blind test.
    data_sha256 identifies the operator's input inventory, linked by evidence_refs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: str = Field(min_length=1, pattern=r"\S")
    attempted_configurations: int = Field(strict=True, ge=1)
    blind_evaluations: int = Field(strict=True, ge=0)
    holdout_evaluations: int = Field(strict=True, ge=1)
    holdout_previously_viewed: Literal[True]
    data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_refs: list[Annotated[str, Field(min_length=1, pattern=r"\S")]] = Field(min_length=1)


def modern_research_audit(
    metadata: ModernExperimentMetadata,
    config: ModernMomentumConfig | None = None,
) -> dict[str, Any]:
    feature_paths = ("research/modern_momentum.py", "research/h30_challenger.py")
    code_paths = (
        *feature_paths,
        "kernel/quote_costs.py",
        "scripts/run_modern_momentum_backtest.py",
        "scripts/run_current_modern_backtest.py",
    )
    code_hashes = {path: sha256_file(ROOT / path) for path in code_paths}
    return {
        "experiment": metadata.model_dump(mode="json"),
        "strategy_manifest": modern_strategy_manifest(config),
        "data_sha256_provenance": "operator-declared input inventory; see evidence_refs",
        "feature_sha256": hashlib.sha256(
            "".join(code_hashes[path] for path in feature_paths).encode()
        ).hexdigest(),
        "code_sha256": hashlib.sha256(json.dumps(code_hashes, sort_keys=True).encode()).hexdigest(),
        "code_file_sha256": code_hashes,
        "holdout_status": "reused_holdout",
        "new_blind_evaluation": False,
        "decision": {
            "stage": "invalid_evidence",
            "reasons": ["Previously viewed holdout is diagnostic, not new blind evidence."],
        },
        "production_eligible": False,
    }


def audit_historical_modern_eligibility(
    trades_path: Path,
    config: ModernMomentumConfig | None = None,
) -> dict[str, Any]:
    """Read-only partial eligibility audit, not a replay or a new performance estimate.

    A stored trade cannot verify the newly shared signal-close eligibility without
    its point-in-time input bars. Passing these label-level checks is not approval.
    No rows are removed and no PnL is reaggregated; old artifacts remain untouched.
    """
    config = config or ModernMomentumConfig()
    trades = pl.read_parquet(trades_path)
    required = {
        "trade_date",
        "symbol",
        "attempt",
        "signal_ts_utc",
        "entry_ts_utc",
        "entry_relative_spread",
        "all_in_stop_pct",
        "premarket_rvol",
        "exit_ts_utc",
        "exit_reason",
    }
    if missing := required - set(trades.columns):
        raise ValueError(f"historical labels missing columns: {sorted(missing)}")
    if any(trades.get_column(name).null_count() for name in required):
        raise ValueError("historical audit fields must not contain nulls")
    keys = ["trade_date", "symbol", "attempt"]
    if trades.unique(subset=keys).height != trades.height:
        raise ValueError("historical attempt identity must be unique")
    counts = {
        name: 0
        for name in (
            "at_or_after_cutoff",
            "spread_exceeds_maximum",
            "all_in_stop_exceeds_maximum",
            "premarket_rvol_below_minimum",
        )
    }
    violations: list[dict[str, Any]] = []
    time_exit_mismatches = 0
    exits_after_liquidation = 0
    for row in trades.iter_rows(named=True):
        if row["attempt"] not in (1, 2):
            raise ValueError("historical attempt must be 1 or 2")
        for name in ("signal_ts_utc", "entry_ts_utc", "exit_ts_utc"):
            if not isinstance(row[name], datetime) or row[name].tzinfo is None:
                raise ValueError("historical timestamps must be timezone-aware")
        for name in ("entry_relative_spread", "all_in_stop_pct", "premarket_rvol"):
            if not isfinite(float(row[name])) or row[name] < 0:
                raise ValueError("historical risk inputs must be finite and nonnegative")
        opened = (
            datetime.combine(
                row["trade_date"], datetime.min.time(), tzinfo=ZoneInfo("America/New_York")
            )
            .replace(hour=9, minute=30)
            .astimezone(UTC)
        )
        cutoff = opened + timedelta(minutes=config.signal_cutoff_minutes)
        liquidation = opened + timedelta(minutes=config.liquidation_minutes)
        failures = {
            "at_or_after_cutoff": max(row["signal_ts_utc"], row["entry_ts_utc"]) >= cutoff,
            "spread_exceeds_maximum": (
                row["entry_relative_spread"] > config.maximum_entry_relative_spread
            ),
            "all_in_stop_exceeds_maximum": (
                row["all_in_stop_pct"] > config.max_all_in_stop_pct + 1e-12
            ),
            "premarket_rvol_below_minimum": (row["premarket_rvol"] < config.minimum_premarket_rvol),
        }
        reasons = [name for name, failed in failures.items() if failed]
        for name in reasons:
            counts[name] += 1
        if reasons:
            violations.append(
                {
                    **{key: row[key] for key in keys},
                    "signal_ts_utc": row["signal_ts_utc"],
                    "entry_ts_utc": row["entry_ts_utc"],
                    "entry_spread_bps": row["entry_relative_spread"] * 10_000,
                    "reasons": reasons,
                }
            )
        time_exit_mismatches += (
            row["exit_reason"] == "time_exit" and row["exit_ts_utc"] != liquidation
        )
        exits_after_liquidation += row["exit_ts_utc"] > liquidation
    return {
        "schema_version": "modern_historical_eligibility_audit.v1",
        "source_path": str(trades_path.resolve()),
        "source_sha256": sha256_file(trades_path),
        "strategy_manifest": modern_strategy_manifest(config),
        "attempts": trades.height,
        "reentries": trades.filter(pl.col("attempt") == 2).height,
        "entry_violations_by_reason": counts,
        "entry_violation_count": len(violations),
        "reentry_violation_count": sum(row["attempt"] == 2 for row in violations),
        "time_exit_labels_requiring_replay": time_exit_mismatches,
        "exits_after_liquidation": exits_after_liquidation,
        "violations": violations,
        "historical_performance_status": "invalidated_for_current_strategy",
        "unverified": [
            "signal-close H15/gap/MACD/volume/cost eligibility requires input bars",
            "market-cap provenance and eligible signals absent from old trade labels",
            "changed exits and portfolio selection require a complete causal replay",
        ],
        "new_performance": None,
        "new_blind_evaluation": False,
        "production_eligible": False,
    }


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
    parser.add_argument("--signals", type=Path)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--audit-trades", type=Path, help="Read-only label audit; JSON to stdout only"
    )
    parser.add_argument("--audit-max-entry-spread", type=float, default=0.0025)
    parser.add_argument(
        "--experiment-metadata",
        type=Path,
        help="JSON identity, cumulative attempt/holdout counts, input hash and evidence references",
    )
    args = parser.parse_args()
    if args.audit_trades:
        if any((args.signals, args.data_root, args.output, args.experiment_metadata)):
            parser.error("--audit-trades cannot be combined with backtest inputs or outputs")
        if not isfinite(args.audit_max_entry_spread) or args.audit_max_entry_spread < 0:
            parser.error("audit spread must be finite and nonnegative")
        audit_config = replace(
            ModernMomentumConfig(), maximum_entry_relative_spread=args.audit_max_entry_spread
        )
        print(
            json.dumps(
                audit_historical_modern_eligibility(args.audit_trades, audit_config),
                indent=2,
                default=str,
            )
        )
        return
    if not all((args.signals, args.data_root, args.output, args.experiment_metadata)):
        parser.error("backtest requires --signals, --data-root, --output and --experiment-metadata")
    trades_path = args.output.with_suffix(".trades.parquet")
    for path in (args.output, trades_path):
        if path.exists():
            raise FileExistsError(f"historical evidence must not be overwritten: {path}")
    metadata = ModernExperimentMetadata.model_validate_json(
        args.experiment_metadata.read_text(encoding="utf-8")
    )
    config = ModernMomentumConfig()
    audit = modern_research_audit(metadata, config)
    load_project_env(ROOT)
    signals = pl.read_parquet(args.signals)
    audit["signals_sha256"] = sha256_file(args.signals)
    bars_by_date, parent_ids = _rth_by_date(args.data_root)
    audit["data_snapshot_ids"] = parent_ids
    dates = sorted(signals.get_column("session_date").unique().to_list())
    schedule_frame = build_xnys_schedule(dates[0] - timedelta(days=10), dates[-1])
    schedule = {row["trade_date"]: row for row in schedule_frame.iter_rows(named=True)}
    all_sessions = schedule_frame.get_column("trade_date").to_list()
    previous = {target: max(item for item in all_sessions if item < target) for target in dates}
    caps = _market_caps(args.data_root)
    missing = {
        (previous[row["session_date"]], str(row["symbol"]))
        for row in signals.select("session_date", "symbol").iter_rows(named=True)
        if (previous[row["session_date"]], str(row["symbol"])) not in caps
    }

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
                raw_exit = trade.exit_px / (1 - entry_spread / 2 - config.market_impact_pct)
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
    holdout_start = trade_dates[int(len(trade_dates) * 0.80)]
    splits = {
        "train": labels.filter(pl.col("trade_date") < train_end),
        "validation": labels.filter(
            (pl.col("trade_date") >= train_end) & (pl.col("trade_date") < holdout_start)
        ),
        "reused_holdout": labels.filter(pl.col("trade_date") >= holdout_start),
    }
    metrics = {name: _metrics(frame) for name, frame in splits.items()}
    metrics["full"] = _metrics(labels)
    episodes = {name: _episode_metrics(frame) for name, frame in splits.items()}
    episodes["full"] = _episode_metrics(labels)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "complete",
        "strategy": audit["strategy_manifest"]["strategy_version"],
        "strategy_manifest": audit["strategy_manifest"],
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
            "reused_holdout_start": holdout_start.isoformat(),
        },
        "cost_model": {
            "spread": "historical Alpaca SIP NBBO",
            "impact_bps_per_side": config.market_impact_pct * 10_000,
            "commission": 0.0,
        },
        "scientific_method": audit,
        "trades_path": str(trades_path),
        "production_eligible": False,
    }
    # Exclusive creation also protects evidence if another run wins the output path race.
    with args.output.open("x", encoding="utf-8") as report:
        with trades_path.open("xb") as trades_file:
            labels.write_parquet(trades_file)
        report.write(json.dumps(payload, indent=2, default=str))
    print(json.dumps(payload, default=str))


if __name__ == "__main__":
    main()
