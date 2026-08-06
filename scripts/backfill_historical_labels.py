from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from dotenv import load_dotenv

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.providers.alpaca import fetch_bars, fetch_quotes
from data_plane.quality import BAR_SCHEMA_VERSION, audit_minute_bars
from data_plane.storage import persist_snapshot, sha256_file
from kernel.backtest import BacktestTrade, backtest_orb_trade
from kernel.config import load_config
from kernel.quote_costs import latest_nbbo_spread, window_nbbo_spread
from kernel.signals import orb5
from research.history import HISTORICAL_SELECTION_PROFILE
from research.validation import purged_walk_forward_splits
from schedule.runtime import ProcessLock

ROOT = Path(__file__).resolve().parents[1]
GATE_INDEX_SOURCE = "research.history.selection_gates_index"
BAR_SOURCE = "alpaca.sip.rth_1m"
QUOTE_SOURCE = "alpaca.sip.nbbo_cost"
CENSUS_SOURCE = "research.history.trade_replay_census"
LABEL_SOURCE = "research.history.net_labels"
OOS_SOURCE = "research.validation.purged_oos_folds"


def _required_float(value: object, name: str) -> float:
    if not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    return float(value)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"manifest is not an object: {path}")
    return value


def _snapshot(path: Path) -> DatasetSnapshot:
    return DatasetSnapshot.model_validate(_manifest(path.parent / "manifest.json"))


def _latest_index(data_root: Path, end_date: date) -> tuple[pl.DataFrame, DatasetSnapshot]:
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(f"{GATE_INDEX_SOURCE}-*/data.parquet"):
        frame = pl.read_parquet(path, columns=["trade_date"])
        if frame["trade_date"].max() != end_date:
            continue
        snapshot = _snapshot(path)
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        raise FileNotFoundError(f"no completed gate index ending {end_date}")
    _, path, snapshot = max(matches)
    return pl.read_parquet(path), snapshot


def _dataset_paths(data_root: Path) -> dict[str, Path]:
    return {
        _snapshot(path).dataset_id: path
        for path in (data_root / "accepted").glob("*/data.parquet")
    }


def _provenance_cache(
    data_root: Path, source: str
) -> dict[str, tuple[Path, DatasetSnapshot]]:
    result: dict[str, tuple[Path, DatasetSnapshot]] = {}
    for path in (data_root / "accepted").glob(f"{source}-*/data.parquet"):
        snapshot = _snapshot(path)
        for check in snapshot.checks:
            current = result.get(check.provenance)
            if current is None or current[1].asof_utc < snapshot.asof_utc:
                result[check.provenance] = (path, snapshot)
    return result


def _hash_symbols(symbols: tuple[str, ...]) -> str:
    return hashlib.sha256(",".join(symbols).encode()).hexdigest()[:16]


def _chunks(values: tuple[str, ...], size: int) -> list[tuple[str, ...]]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    return [values[index : index + size] for index in range(0, len(values), size)]


def _check(
    name: str,
    passed: bool,
    observed: object,
    expected: str,
    provenance: str,
    *,
    severity: QualitySeverity = QualitySeverity.CRITICAL,
) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=severity,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance=provenance,
    )


def _bar_provenance(
    trade_date: date, symbols: tuple[str, ...], start_utc: datetime, end_utc: datetime
) -> str:
    return (
        f"{BAR_SOURCE}@{trade_date.isoformat()}|{start_utc.isoformat()}.."
        f"{end_utc.isoformat()}|symbols_sha256={_hash_symbols(symbols)}"
    )


def _bar_checks(
    frame: pl.DataFrame,
    *,
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
    provenance: str,
) -> tuple[DataQualityCheck, ...]:
    checks = list(
        audit_minute_bars(
            frame,
            provenance=provenance,
            expected_symbols=(),
            research_approved=True,
        )
    )
    outside_symbols = set(frame["symbol"].to_list()) - set(symbols)
    outside_window = frame.filter(
        (pl.col("ts_utc") < start_utc) | (pl.col("ts_utc") >= end_utc)
    ).height
    checks.extend(
        (
            _check("provider_query_completed", True, True, "complete", provenance),
            _check(
                "requested_symbol_scope",
                not outside_symbols,
                len(outside_symbols),
                "0",
                provenance,
            ),
            _check(
                "half_open_session_window",
                outside_window == 0,
                outside_window,
                "0",
                provenance,
            ),
        )
    )
    return tuple(checks)


def _quote_provenance(
    trade_date: date,
    symbol: str,
    entry_ts: datetime,
    exit_ts: datetime,
) -> str:
    return (
        f"{QUOTE_SOURCE}@{trade_date.isoformat()}:{symbol}|entry={entry_ts.isoformat()}|"
        f"exit_minute={exit_ts.isoformat()}"
    )


def _quote_checks(
    frame: pl.DataFrame,
    *,
    symbol: str,
    windows: tuple[tuple[datetime, datetime], ...],
    provenance: str,
) -> tuple[DataQualityCheck, ...]:
    duplicates = frame.height - frame.select(pl.struct("symbol", "ts_utc").n_unique()).item()
    outside_symbol = frame.filter(pl.col("symbol") != symbol).height
    in_window = pl.lit(False)
    for start_utc, end_utc in windows:
        in_window |= (pl.col("ts_utc") >= start_utc) & (pl.col("ts_utc") < end_utc)
    outside_window = frame.filter(~in_window).height
    crossed = frame.filter(
        pl.col("bid_price").is_finite()
        & pl.col("ask_price").is_finite()
        & (pl.col("bid_price") > pl.col("ask_price"))
    ).height
    return (
        _check("provider_query_completed", True, True, "complete", provenance),
        _check("unique_symbol_timestamp", duplicates == 0, duplicates, "0", provenance),
        _check("requested_symbol_scope", outside_symbol == 0, outside_symbol, "0", provenance),
        _check("requested_windows", outside_window == 0, outside_window, "0", provenance),
        _check(
            "crossed_quotes_excluded_from_cost",
            crossed == 0,
            crossed,
            "0",
            provenance,
            severity=QualitySeverity.WARNING,
        ),
    )


def _code_hash() -> str:
    digest = hashlib.sha256()
    for relative in (
        "kernel/backtest.py",
        "kernel/signals.py",
        "kernel/labels.py",
        "kernel/quote_costs.py",
        "research/validation.py",
    ):
        digest.update(relative.encode())
        digest.update((ROOT / relative).read_bytes())
    return digest.hexdigest()


def _trade_row(
    trade: BacktestTrade,
    *,
    trade_date: date,
    candidate: dict[str, object],
    entry_spread: float,
    exit_spread: float,
    quote_snapshot_id: str,
    bar_snapshot_id: str,
    gate_snapshot_id: str,
    entry_quote_provenance: str,
    exit_quote_provenance: str,
) -> dict[str, object]:
    return {
        "trade_date": trade_date,
        "symbol": trade.symbol,
        "selection_rank": candidate["selection_rank"],
        "selection_profile": HISTORICAL_SELECTION_PROFILE,
        "rvol": candidate["rvol"],
        "atr14": _required_float(candidate["price"], "price")
        * _required_float(candidate["atr_pct"], "atr_pct"),
        "adv_usd": candidate["adv_usd"],
        "tier": candidate["tier"],
        "confidence": 1.0,
        "entry_ts_utc": trade.signal.entry_ts_utc,
        "entry_px": trade.signal.entry_px,
        "exit_ts_utc": trade.barrier.exit_ts,
        "exit_px": trade.barrier.exit_px,
        "barrier": trade.barrier.which,
        "shares": trade.sizing.shares,
        "entry_relative_spread": entry_spread,
        "exit_relative_spread_p95": exit_spread,
        "cost_relative_spread": max(entry_spread, exit_spread),
        "commission": trade.costs.commission,
        "sec_fee": trade.costs.sec_fee,
        "finra_taf": trade.costs.finra_taf,
        "spread_cost": trade.costs.spread,
        "impact_cost": trade.costs.impact,
        "stop_slippage_cost": trade.costs.stop_slippage,
        "total_cost": trade.costs.total,
        "gross_pnl": trade.gross_pnl,
        "net_pnl": trade.net_pnl,
        "net_return_on_notional": trade.net_return_on_notional,
        "entry_quote_provenance": entry_quote_provenance,
        "exit_quote_provenance": exit_quote_provenance,
        "gate_snapshot_id": gate_snapshot_id,
        "bar_snapshot_id": bar_snapshot_id,
        "quote_snapshot_id": quote_snapshot_id,
        "provenance": trade.provenance + "|confidence=neutral_1.0|spread=max(entry,exit_p95)",
    }


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", type=_parse_date, required=True)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--symbol-chunk-size", type=int, default=200)
    args = parser.parse_args()

    gate_index, gate_index_snapshot = _latest_index(args.data_root, args.end)
    paths = _dataset_paths(args.data_root)
    cfg = load_config(ROOT / "config.yaml")
    first_trade_date = gate_index["trade_date"].min()
    if not isinstance(first_trade_date, date):
        raise ValueError("gate index has no valid first trade date")
    schedule = build_xnys_schedule(first_trade_date, args.end)
    bar_cache = _provenance_cache(args.data_root, BAR_SOURCE)
    quote_cache = _provenance_cache(args.data_root, QUOTE_SOURCE)
    census: list[dict[str, object]] = []
    labels: list[dict[str, object]] = []

    with ProcessLock(ROOT / "runs" / "historical-labels.lock"):
        for index_number, index_row in enumerate(gate_index.iter_rows(named=True), start=1):
            trade_date = index_row["trade_date"]
            if not isinstance(trade_date, date):
                raise ValueError("gate index trade date is invalid")
            gate_snapshot_id = str(index_row["selection_gate_snapshot_id"])
            gate_path = paths[gate_snapshot_id]
            gate_snapshot = _snapshot(gate_path)
            selection = pl.read_parquet(gate_path).sort("selection_rank", "symbol")
            survivors = selection.filter(pl.col("pass_gate"))
            # Label every gate survivor so later allowlisted threshold experiments can
            # reconstruct their own top-N portfolio without survivorship-by-baseline.
            # Capacity is applied inside each OOS fold, never before counterfactuals.
            actionable = survivors
            symbols = tuple(actionable["symbol"].to_list())
            session = schedule.filter(pl.col("trade_date") == trade_date).row(0, named=True)
            market_open = session["market_open_utc"]
            market_close = session["market_close_utc"]
            if not isinstance(market_open, datetime) or not isinstance(market_close, datetime):
                raise ValueError("calendar session timestamps are invalid")
            if not symbols:
                print(
                    json.dumps(
                        {
                            "event": "label_session_complete",
                            "completed": index_number,
                            "total": gate_index.height,
                            "trade_date": trade_date.isoformat(),
                            "actionable": 0,
                            "labels": 0,
                        }
                    ),
                    flush=True,
                )
                continue

            bar_provenance = _bar_provenance(
                trade_date, symbols, market_open, market_close
            )
            cached_bar = bar_cache.get(bar_provenance)
            if cached_bar is None:
                parts = [
                    fetch_bars(chunk, market_open, market_close)
                    for chunk in _chunks(symbols, args.symbol_chunk_size)
                ]
                bars = pl.concat(parts) if len(parts) > 1 else parts[0]
                bar_snapshot, _ = persist_snapshot(
                    bars,
                    root=args.data_root,
                    source=BAR_SOURCE,
                    schema_version=BAR_SCHEMA_VERSION,
                    checks=_bar_checks(
                        bars,
                        symbols=symbols,
                        start_utc=market_open,
                        end_utc=market_close,
                        provenance=bar_provenance,
                    ),
                    parent_snapshot_ids=(gate_snapshot.dataset_id,),
                )
                bar_snapshot.assert_usable()
            else:
                bar_path, bar_snapshot = cached_bar
                bars = pl.read_parquet(bar_path)

            session_label_count = 0
            for candidate in actionable.iter_rows(named=True):
                symbol = str(candidate["symbol"])
                symbol_bars = bars.filter(pl.col("symbol") == symbol)
                signal = orb5(
                    symbol_bars,
                    session_open_utc=market_open,
                    asof_utc=market_close,
                    rvol=float(candidate["rvol"]),
                    min_rvol=cfg.universe.min_rvol,
                )
                if not signal.triggered:
                    census.append(
                        {
                            "trade_date": trade_date,
                            "symbol": symbol,
                            "selection_rank": candidate["selection_rank"],
                            "status": "no_orb_signal",
                            "detail": signal.reason,
                            "gate_snapshot_id": gate_snapshot_id,
                        }
                    )
                    continue
                try:
                    preliminary = backtest_orb_trade(
                        symbol_bars,
                        symbol=symbol,
                        trade_date=trade_date,
                        session_open_utc=market_open,
                        session_close_utc=market_close,
                        is_half_day=bool(session["is_half_day"]),
                        rvol=float(candidate["rvol"]),
                        atr14=float(candidate["price"]) * float(candidate["atr_pct"]),
                        adv_usd=float(candidate["adv_usd"]),
                        tier=str(candidate["tier"]),
                        confidence=1.0,
                        cs_spread=0.0,
                        cfg=cfg,
                    )
                except ValueError as exc:
                    census.append(
                        {
                            "trade_date": trade_date,
                            "symbol": symbol,
                            "selection_rank": candidate["selection_rank"],
                            "status": "censored_bar_path",
                            "detail": str(exc),
                            "gate_snapshot_id": gate_snapshot_id,
                        }
                    )
                    continue
                if preliminary is None:
                    census.append(
                        {
                            "trade_date": trade_date,
                            "symbol": symbol,
                            "selection_rank": candidate["selection_rank"],
                            "status": "zero_sizing",
                            "detail": "deterministic sizing returned no shares",
                            "gate_snapshot_id": gate_snapshot_id,
                        }
                    )
                    continue
                entry_ts = preliminary.signal.entry_ts_utc
                if entry_ts is None:
                    raise AssertionError("triggered preliminary trade has no entry timestamp")
                exit_ts = preliminary.barrier.exit_ts
                entry_window = (
                    entry_ts - timedelta(seconds=30),
                    entry_ts + timedelta(milliseconds=1),
                )
                exit_window = (exit_ts, exit_ts + timedelta(minutes=1))
                quote_provenance = _quote_provenance(
                    trade_date, symbol, entry_ts, exit_ts
                )
                cached_quote = quote_cache.get(quote_provenance)
                if cached_quote is None:
                    quote_parts = [
                        fetch_quotes((symbol,), *entry_window),
                        fetch_quotes((symbol,), *exit_window),
                    ]
                    quotes = (
                        pl.concat(quote_parts)
                        .unique(subset=["symbol", "ts_utc"], keep="first")
                        .sort("symbol", "ts_utc")
                    )
                    quote_snapshot, _ = persist_snapshot(
                        quotes,
                        root=args.data_root,
                        source=QUOTE_SOURCE,
                        schema_version="sip_nbbo_quotes.v1",
                        checks=_quote_checks(
                            quotes,
                            symbol=symbol,
                            windows=(entry_window, exit_window),
                            provenance=quote_provenance,
                        ),
                        parent_snapshot_ids=(
                            gate_snapshot.dataset_id,
                            bar_snapshot.dataset_id,
                        ),
                    )
                    quote_snapshot.assert_usable()
                else:
                    quote_path, quote_snapshot = cached_quote
                    quotes = pl.read_parquet(quote_path)
                entry_quote = latest_nbbo_spread(
                    quotes,
                    symbol=symbol,
                    at_utc=entry_ts,
                    max_age=timedelta(seconds=30),
                )
                exit_quote = window_nbbo_spread(
                    quotes,
                    symbol=symbol,
                    start_utc=exit_window[0],
                    end_utc=exit_window[1],
                    quantile=0.95,
                )
                if entry_quote is None or exit_quote is None:
                    census.append(
                        {
                            "trade_date": trade_date,
                            "symbol": symbol,
                            "selection_rank": candidate["selection_rank"],
                            "status": "quote_unavailable",
                            "detail": (
                                f"entry={entry_quote is not None};"
                                f"exit={exit_quote is not None}"
                            ),
                            "gate_snapshot_id": gate_snapshot_id,
                        }
                    )
                    continue
                observed_spread = max(
                    entry_quote.relative_spread, exit_quote.relative_spread
                )
                trade = backtest_orb_trade(
                    symbol_bars,
                    symbol=symbol,
                    trade_date=trade_date,
                    session_open_utc=market_open,
                    session_close_utc=market_close,
                    is_half_day=bool(session["is_half_day"]),
                    rvol=float(candidate["rvol"]),
                    atr14=float(candidate["price"]) * float(candidate["atr_pct"]),
                    adv_usd=float(candidate["adv_usd"]),
                    tier=str(candidate["tier"]),
                    confidence=1.0,
                    cs_spread=observed_spread,
                    cfg=cfg,
                )
                if trade is None:
                    raise AssertionError("cost-only replay changed a preliminary trade")
                labels.append(
                    _trade_row(
                        trade,
                        trade_date=trade_date,
                        candidate=candidate,
                        entry_spread=entry_quote.relative_spread,
                        exit_spread=exit_quote.relative_spread,
                        quote_snapshot_id=quote_snapshot.dataset_id,
                        bar_snapshot_id=bar_snapshot.dataset_id,
                        gate_snapshot_id=gate_snapshot_id,
                        entry_quote_provenance=entry_quote.provenance,
                        exit_quote_provenance=exit_quote.provenance,
                    )
                )
                census.append(
                    {
                        "trade_date": trade_date,
                        "symbol": symbol,
                        "selection_rank": candidate["selection_rank"],
                        "status": "labeled",
                        "detail": quote_snapshot.dataset_id,
                        "gate_snapshot_id": gate_snapshot_id,
                    }
                )
                session_label_count += 1

            print(
                json.dumps(
                    {
                        "event": "label_session_complete",
                        "completed": index_number,
                        "total": gate_index.height,
                        "trade_date": trade_date.isoformat(),
                        "actionable": len(symbols),
                        "labels": session_label_count,
                    }
                ),
                flush=True,
            )

    census_frame = pl.DataFrame(census).with_columns(pl.col("trade_date").cast(pl.Date))
    census_duplicates = census_frame.height - census_frame.select(
        pl.struct("trade_date", "symbol").n_unique()
    ).item()
    census_checks = (
        _check("unique_trade", census_duplicates == 0, census_duplicates, "0", CENSUS_SOURCE),
        _check("non_empty", census_frame.height > 0, census_frame.height, ">0", CENSUS_SOURCE),
    )
    census_snapshot, _ = persist_snapshot(
        census_frame,
        root=args.data_root,
        source=CENSUS_SOURCE,
        schema_version="trade_replay_census.v1",
        checks=census_checks,
        parent_snapshot_ids=(gate_index_snapshot.dataset_id,),
    )
    census_snapshot.assert_usable()
    if not labels:
        raise RuntimeError("historical replay produced zero cost-complete labels")
    label_frame = pl.DataFrame(labels).with_columns(pl.col("trade_date").cast(pl.Date)).sort(
        "trade_date", "selection_rank", "symbol"
    )
    label_duplicates = label_frame.height - label_frame.select(
        pl.struct("trade_date", "symbol").n_unique()
    ).item()
    triggered_uncensored = census_frame.filter(
        pl.col("status").is_in(["labeled", "quote_unavailable"])
    ).height
    quote_coverage = label_frame.height / triggered_uncensored if triggered_uncensored else 0.0
    label_checks = (
        _check("unique_trade", label_duplicates == 0, label_duplicates, "0", LABEL_SOURCE),
        _check(
            "cost_complete",
            label_frame["total_cost"].is_not_null().all(),
            True,
            "all",
            LABEL_SOURCE,
        ),
        _check(
            "quote_provenance",
            label_frame["quote_snapshot_id"].is_not_null().all(),
            True,
            "all",
            LABEL_SOURCE,
        ),
        _check(
            "minimum_label_target",
            label_frame.height >= 100,
            label_frame.height,
            ">=100",
            LABEL_SOURCE,
            severity=QualitySeverity.WARNING,
        ),
        _check(
            "quote_cost_coverage",
            quote_coverage >= 0.99,
            f"{quote_coverage:.6f}",
            ">=0.99",
            LABEL_SOURCE,
            severity=QualitySeverity.WARNING,
        ),
    )
    label_snapshot, label_path = persist_snapshot(
        label_frame,
        root=args.data_root,
        source=LABEL_SOURCE,
        schema_version="net_cost_complete_labels.v1",
        checks=label_checks,
        parent_snapshot_ids=(census_snapshot.dataset_id,),
    )
    label_snapshot.assert_usable()

    folds = purged_walk_forward_splits(
        np.array(label_frame["trade_date"].to_list(), dtype=object),
        n_splits=5,
        purge_days=1,
        embargo_days=2,
    )
    config_hash = sha256_file(ROOT / "config.yaml")
    code_hash = _code_hash()
    feature_hash = hashlib.sha256(
        b"massive_news_only.v1|rvol20_median|orb5|triple_barrier|nbbo_cost.v1"
    ).hexdigest()
    fold_rows: list[dict[str, object]] = []
    for fold_number, fold in enumerate(folds, start=1):
        validation_candidates = (
            label_frame.with_row_index("_row")
            .filter(pl.col("_row").is_in(fold.validation_indices.tolist()))
            .drop("_row")
        )
        validation = (
            validation_candidates.sort("trade_date", "selection_rank", "symbol")
            .group_by("trade_date", maintain_order=True)
            .head(cfg.max_concurrent)
        )
        wins = validation.filter(pl.col("net_pnl") > 0).height
        gains = float(validation.filter(pl.col("net_pnl") > 0)["net_pnl"].sum() or 0)
        losses = abs(float(validation.filter(pl.col("net_pnl") < 0)["net_pnl"].sum() or 0))
        fold_rows.append(
            {
                "fold": fold_number,
                "train_rows": len(fold.train_indices),
                "validation_rows": len(fold.validation_indices),
                "validation_start": fold.validation_start,
                "validation_end": fold.validation_end,
                "embargo_end": fold.embargo_end,
                "net_pnl": float(validation["net_pnl"].sum()),
                "mean_net_return": _required_float(
                    validation["net_return_on_notional"].mean(), "mean_net_return"
                ),
                "win_rate": wins / validation.height,
                "profit_factor": gains / losses if losses > 0 else None,
                "attempted_configurations": 1,
                "random_seed": 0,
                "data_snapshot_id": label_snapshot.dataset_id,
                "data_sha256": label_snapshot.content_sha256,
                "feature_set_sha256": feature_hash,
                "config_sha256": config_hash,
                "code_sha256": code_hash,
                "selection_profile": HISTORICAL_SELECTION_PROFILE,
            }
        )
    fold_frame = pl.DataFrame(fold_rows).with_columns(
        pl.col("validation_start").cast(pl.Date),
        pl.col("validation_end").cast(pl.Date),
        pl.col("embargo_end").cast(pl.Date),
    )
    empty_validation = fold_frame.filter(pl.col("validation_rows") <= 0).height
    multiple_configurations = fold_frame.filter(
        pl.col("attempted_configurations") != 1
    ).height
    fold_checks = (
        _check("exact_fold_count", fold_frame.height == 5, fold_frame.height, "5", OOS_SOURCE),
        _check(
            "non_empty_validation",
            empty_validation == 0,
            empty_validation,
            "0",
            OOS_SOURCE,
        ),
        _check(
            "one_frozen_configuration",
            multiple_configurations == 0,
            multiple_configurations,
            "0",
            OOS_SOURCE,
        ),
    )
    fold_snapshot, fold_path = persist_snapshot(
        fold_frame,
        root=args.data_root,
        source=OOS_SOURCE,
        schema_version="purged_walk_forward_metrics.v1",
        checks=fold_checks,
        parent_snapshot_ids=(label_snapshot.dataset_id,),
    )
    fold_snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "census": census_frame.height,
                "labels": label_frame.height,
                "quote_cost_coverage": quote_coverage,
                "oos_folds": fold_frame.height,
                "label_dataset_id": label_snapshot.dataset_id,
                "label_path": str(label_path),
                "fold_dataset_id": fold_snapshot.dataset_id,
                "fold_path": str(fold_path),
            }
        )
    )


if __name__ == "__main__":
    main()
