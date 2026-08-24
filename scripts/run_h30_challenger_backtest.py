"""Run frozen H30 baseline and EMA-soft-score research variants."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.storage import persist_snapshot, sha256_file
from research.h30_challenger import (
    H30Config,
    H30PathResult,
    assess_h30_challenger,
    evaluate_h30_path,
)
from research.validation import purged_walk_forward_splits

ROOT = Path(__file__).resolve().parents[1]
CENSUS_SOURCE = "research.h30_challenger.census"
LABEL_SOURCE = "research.h30_challenger.labels"
METRIC_SOURCE = "research.h30_challenger.metrics"
DECISION_SOURCE = "research.h30_challenger.decision"
SECTOR_SOURCE = "research.h30_sector_classification"
NBBO_SOURCE = "research.h30_signal_nbbo.evidence"
RISK_UNIT_USD = 10_000.0
DAILY_RISK_LIMIT_USD = 30_000.0
PORTFOLIO_NOTIONAL_LIMIT_USD = 2_000_000.0
MAX_SYMBOLS_PER_DAY = 3
COMMISSION_PER_SHARE_ROUND_TRIP = 0.007
VARIANTS = (
    "baseline_no_ema",
    "ema_soft_score",
    "ema_sector_strength",
    "early_entry_before_noon",
    "early_dual_route",
)
EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class _CandidateEvidence:
    rank: int
    result: H30PathResult
    dual_result: H30PathResult
    sector_proxy: str | None
    stock_return: float | None
    sector_return: float | None
    market_return: float | None
    sector_strength_passed: bool


def _manifest(path: Path) -> DatasetSnapshot:
    return DatasetSnapshot.model_validate_json(
        (path.parent / "manifest.json").read_text(encoding="utf-8")
    )


def _check(
    name: str,
    passed: bool,
    observed: Any,
    expected: str,
    *,
    severity: QualitySeverity = QualitySeverity.CRITICAL,
) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=severity,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance="scripts.run_h30_challenger_backtest.v1",
    )


def _load_episodes(data_root: Path) -> tuple[pl.DataFrame, tuple[str, ...]]:
    frames: list[pl.DataFrame] = []
    parents: list[str] = []
    for path in sorted((data_root / "accepted").glob("research.trading_episodes-*/data.parquet")):
        frames.append(pl.read_parquet(path))
        parents.append(_manifest(path).dataset_id)
    if not frames:
        raise FileNotFoundError("trading episodes are missing")
    return (
        pl.concat(frames, how="diagonal_relaxed")
        .filter(pl.col("market_cap") >= 1_000_000_000)
        .sort("session_date", "selection_rank", "symbol")
        .unique(("session_date", "symbol"), keep="last"),
        tuple(parents),
    )


def _load_cohort(data_root: Path) -> tuple[pl.DataFrame, tuple[str, ...]]:
    matches = [
        (_manifest(path).asof_utc, path, _manifest(path))
        for path in (data_root / "accepted").glob(
            "research.h30_candidate_cohort-*/data.parquet"
        )
    ]
    if not matches:
        raise FileNotFoundError("H30 candidate cohort is missing")
    _, path, snapshot = max(matches, key=lambda item: item[0])
    frame = pl.read_parquet(path).select(
        "session_date", "symbol", "selection_rank", "market_cap"
    )
    return frame, (snapshot.dataset_id,)


def _rth_by_date(
    data_root: Path,
) -> tuple[dict[date, pl.DataFrame], tuple[str, ...]]:
    result: dict[date, pl.DataFrame] = {}
    parents: list[str] = []
    for path in sorted((data_root / "accepted").glob("alpaca.sip.rth_1m-*/data.parquet")):
        frame = pl.read_parquet(path)
        dates = (
            frame.get_column("ts_utc")
            .dt.convert_time_zone("America/New_York")
            .dt.date()
            .unique()
            .to_list()
        )
        if len(dates) != 1:
            raise ValueError(f"RTH snapshot must contain one session: {path}")
        result[dates[0]] = frame
        parents.append(_manifest(path).dataset_id)
    if not result:
        raise FileNotFoundError("historical RTH snapshots are missing")
    return result, tuple(parents)


def _latest_optional(data_root: Path, source: str) -> tuple[pl.DataFrame, DatasetSnapshot] | None:
    matches = [
        (_manifest(path).asof_utc, path, _manifest(path))
        for path in (data_root / "accepted").glob(f"{source}-*/data.parquet")
    ]
    if not matches:
        return None
    _, path, snapshot = max(matches, key=lambda item: item[0])
    return pl.read_parquet(path), snapshot


def _return_at(
    bars: pl.DataFrame,
    *,
    symbol: str,
    session_open_utc: object,
    at_utc: object,
) -> float | None:
    if not isinstance(session_open_utc, datetime) or not isinstance(at_utc, datetime):
        return None
    frame = bars.filter(
        (pl.col("symbol") == symbol)
        & (pl.col("ts_utc") >= session_open_utc)
        & (pl.col("ts_utc") < at_utc)
    ).sort("ts_utc")
    if frame.is_empty():
        return None
    opening = _number(frame.row(0, named=True)["open"], "relative-strength open")
    close = _number(frame.row(-1, named=True)["close"], "relative-strength close")
    return close / opening - 1


def _number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    return float(value)


def _daily_metrics(labels: pl.DataFrame, variant: str) -> dict[str, object]:
    frame = labels.filter(pl.col("variant") == variant)
    daily = frame.group_by("trade_date").agg(pl.col("net_pnl").sum()).sort("trade_date")
    pnl = frame.get_column("net_pnl")
    gains = float(pnl.filter(pnl > 0).sum() or 0)
    losses = abs(float(pnl.filter(pnl < 0).sum() or 0))
    curve = np.cumsum(daily.get_column("net_pnl").to_numpy())
    peak = np.maximum.accumulate(np.insert(curve, 0, 0.0))[1:]
    drawdown = curve - peak
    return {
        "variant": variant,
        "trade_legs": frame.height,
        "symbols_traded": frame.select("trade_date", "symbol").unique().height,
        "net_pnl": _number(pnl.sum(), "net_pnl"),
        "win_rate": frame.filter(pl.col("net_pnl") > 0).height / frame.height,
        "profit_factor": gains / losses if losses else None,
        "max_drawdown_usd": float(drawdown.min()) if drawdown.size else 0.0,
        "mean_return_pct": _number(
            frame.get_column("return_pct").mean(), "mean_return_pct"
        ),
    }


def _fold_metrics(labels: pl.DataFrame) -> list[dict[str, object]]:
    ordered = labels.sort("trade_date", "variant", "symbol", "attempt")
    folds = purged_walk_forward_splits(
        np.array(ordered.get_column("trade_date").to_list(), dtype=object),
        n_splits=5,
        purge_days=1,
        embargo_days=2,
    )
    rows: list[dict[str, object]] = []
    for number, fold in enumerate(folds, start=1):
        validation = ordered.with_row_index("_row").filter(
            pl.col("_row").is_in(fold.validation_indices.tolist())
        )
        for variant in VARIANTS:
            frame = validation.filter(pl.col("variant") == variant)
            rows.append(
                {
                    "fold": number,
                    "variant": variant,
                    "validation_start": fold.validation_start,
                    "validation_end": fold.validation_end,
                    "trade_legs": frame.height,
                    "net_pnl": float(frame.get_column("net_pnl").sum()),
                    "win_rate": (
                        frame.filter(pl.col("net_pnl") > 0).height / frame.height
                        if frame.height
                        else None
                    ),
                }
            )
    return rows


def _census_row(day: date, rank: int, result: H30PathResult) -> dict[str, object]:
    return {
        "trade_date": day,
        "symbol": result.symbol,
        "selection_rank": rank,
        "status": result.status,
        "reason": result.reason,
        "branch": result.branch,
        "entry_route": result.entry_route,
        "h30": result.h30,
        "l30": result.l30,
        "ema_score": result.ema_score,
        "entry_ts_utc": result.entry_ts_utc,
        "entry_px": result.entry_px,
        "attempt_count": len(result.legs),
        "provenance": result.provenance,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", type=Path, default=ROOT / "runtime" / "ai-quant" / "data"
    )
    parser.add_argument(
        "--candidate-source",
        choices=("episodes", "h30-cohort"),
        default="episodes",
    )
    args = parser.parse_args()
    episodes, episode_parents = (
        _load_cohort(args.data_root)
        if args.candidate_source == "h30-cohort"
        else _load_episodes(args.data_root)
    )
    bars_by_date, bar_parents = _rth_by_date(args.data_root)
    sector_data = _latest_optional(args.data_root, SECTOR_SOURCE)
    nbbo_data = _latest_optional(args.data_root, NBBO_SOURCE)
    sector_frame = sector_data[0] if sector_data else pl.DataFrame()
    nbbo_frame = nbbo_data[0] if nbbo_data else pl.DataFrame()
    sector_map = (
        {str(row["symbol"]): row["sector_proxy"] for row in sector_frame.iter_rows(named=True)}
        if not sector_frame.is_empty()
        else {}
    )
    nbbo_map = (
        {
            (
                row["trade_date"],
                str(row["symbol"]),
                int(row["attempt"]),
                row["entry_ts_utc"],
                row["exit_ts_utc"],
            ): row
            for row in nbbo_frame.iter_rows(named=True)
        }
        if not nbbo_frame.is_empty()
        else {}
    )
    start = episodes.get_column("session_date").min()
    end = episodes.get_column("session_date").max()
    if not isinstance(start, date) or not isinstance(end, date):
        raise ValueError("episode date range is invalid")
    schedule = {
        row["trade_date"]: row
        for row in build_xnys_schedule(start, end).iter_rows(named=True)
    }
    census: list[dict[str, object]] = []
    candidates: dict[date, list[_CandidateEvidence]] = {}
    for episode in episodes.iter_rows(named=True):
        day = episode["session_date"]
        symbol = str(episode["symbol"])
        session = schedule.get(day)
        frame = bars_by_date.get(day, pl.DataFrame()).filter(pl.col("symbol") == symbol)
        if session is None or frame.is_empty():
            result = H30PathResult(
                symbol,
                "blocked",
                "session_bars_missing",
                None,
                None,
                None,
                None,
                0,
                0,
                None,
                None,
                (),
                "research.h30_challenger.v1|production=false",
            )
            dual_result = result
        else:
            result = evaluate_h30_path(
                frame, session_open_utc=session["market_open_utc"]
            )
            dual_result = evaluate_h30_path(
                frame,
                session_open_utc=session["market_open_utc"],
                cfg=H30Config(
                    allow_trend_continuation=True,
                    entry_cutoff_minutes=150,
                ),
            )
        rank = int(episode["selection_rank"])
        sector_proxy = sector_map.get(symbol)
        day_bars = bars_by_date.get(day, pl.DataFrame())
        stock_return = sector_return = market_return = None
        if (
            result.status == "traded"
            and session is not None
            and result.entry_ts_utc is not None
            and isinstance(sector_proxy, str)
        ):
            stock_return = _return_at(
                day_bars,
                symbol=symbol,
                session_open_utc=session["market_open_utc"],
                at_utc=result.entry_ts_utc,
            )
            sector_return = _return_at(
                day_bars,
                symbol=sector_proxy,
                session_open_utc=session["market_open_utc"],
                at_utc=result.entry_ts_utc,
            )
            market_return = _return_at(
                day_bars,
                symbol="SPY",
                session_open_utc=session["market_open_utc"],
                at_utc=result.entry_ts_utc,
            )
        sector_passed = (
            stock_return is not None
            and sector_return is not None
            and market_return is not None
            and stock_return > sector_return > market_return
        )
        census_row = _census_row(day, rank, result)
        census_row.update(
            {
                "sector_proxy": sector_proxy,
                "stock_return_at_entry": stock_return,
                "sector_return_at_entry": sector_return,
                "market_return_at_entry": market_return,
                "sector_strength_passed": sector_passed,
                "dual_status": dual_result.status,
                "dual_reason": dual_result.reason,
                "dual_entry_route": dual_result.entry_route,
                "dual_entry_ts_utc": dual_result.entry_ts_utc,
            }
        )
        census.append(census_row)
        if result.status == "traded" or dual_result.status == "traded":
            candidates.setdefault(day, []).append(
                _CandidateEvidence(
                    rank,
                    result,
                    dual_result,
                    sector_proxy if isinstance(sector_proxy, str) else None,
                    stock_return,
                    sector_return,
                    market_return,
                    sector_passed,
                )
            )

    labels: list[dict[str, object]] = []
    for day, values in sorted(candidates.items()):
        for variant in VARIANTS:
            if variant == "ema_sector_strength":
                eligible = [value for value in values if value.sector_strength_passed]
            elif variant == "early_entry_before_noon":
                eligible = [
                    value
                    for value in values
                    if value.result.entry_ts_utc is not None
                    and value.result.entry_ts_utc.astimezone(EASTERN).hour < 12
                ]
            elif variant == "early_dual_route":
                eligible = [
                    value for value in values if value.dual_result.status == "traded"
                ]
            else:
                eligible = [value for value in values if value.result.status == "traded"]
            selected = sorted(
                eligible,
                key=lambda item: (
                    item.rank,
                    (
                        item.dual_result.symbol
                        if variant == "early_dual_route"
                        else item.result.symbol
                    )
                    or "",
                ),
            )[:MAX_SYMBOLS_PER_DAY]
            remaining_risk = DAILY_RISK_LIMIT_USD
            remaining_notional = PORTFOLIO_NOTIONAL_LIMIT_USD
            for evidence in selected:
                rank = evidence.rank
                result = (
                    evidence.dual_result
                    if variant == "early_dual_route"
                    else evidence.result
                )
                for attempt, leg in enumerate(result.legs, start=1):
                    if variant in {
                        "baseline_no_ema",
                        "early_entry_before_noon",
                        "early_dual_route",
                    }:
                        fraction = 0.5 if attempt == 1 else 0.25
                    else:
                        fraction = leg.risk_fraction
                    quote = nbbo_map.get(
                        (
                            day,
                            str(result.symbol),
                            attempt,
                            leg.entry_ts_utc,
                            leg.exit_ts_utc,
                        )
                    )
                    entry_spread = None if quote is None else quote["entry_relative_spread"]
                    exit_spread = None if quote is None else quote["exit_relative_spread_p95"]
                    if isinstance(entry_spread, (int, float)) and isinstance(
                        exit_spread, (int, float)
                    ):
                        entry_px = leg.entry_px / 1.001 * (1 + float(entry_spread) / 2)
                        if leg.exit_reason == "fixed_stop_including_slippage":
                            exit_px = entry_px * 0.98
                        else:
                            exit_px = leg.exit_px / 0.999 * (1 - float(exit_spread) / 2)
                        cost_status = "observed_nbbo_spread_no_market_impact"
                    else:
                        entry_px, exit_px = leg.entry_px, leg.exit_px
                        cost_status = "nbbo_window_missing"
                    risk_budget = min(remaining_risk, RISK_UNIT_USD * fraction)
                    risk_per_share = entry_px * 0.02
                    shares = min(
                        int(risk_budget / risk_per_share),
                        int(remaining_notional / entry_px),
                    )
                    if shares <= 0:
                        continue
                    commission = max(0.70, shares * COMMISSION_PER_SHARE_ROUND_TRIP)
                    net_pnl = shares * (exit_px - entry_px) - commission
                    used_risk = shares * risk_per_share
                    notional = shares * entry_px
                    remaining_risk -= used_risk
                    remaining_notional -= notional
                    session = schedule[day]
                    path = bars_by_date[day].filter(
                        (pl.col("symbol") == result.symbol)
                        & (pl.col("ts_utc") >= leg.entry_ts_utc)
                        & (pl.col("ts_utc") < leg.exit_ts_utc)
                    )
                    pre_entry = bars_by_date[day].filter(
                        (pl.col("symbol") == result.symbol)
                        & (pl.col("ts_utc") >= session["market_open_utc"])
                        & (pl.col("ts_utc") < leg.entry_ts_utc)
                    ).sort("ts_utc")
                    opening_px = (
                        None
                        if pre_entry.is_empty()
                        else _number(pre_entry.row(0, named=True)["open"], "session open")
                    )
                    mfe = (
                        None
                        if path.is_empty()
                        else _number(path.get_column("high").max(), "path high") / entry_px
                        - 1
                    )
                    mae = (
                        None
                        if path.is_empty()
                        else _number(path.get_column("low").min(), "path low") / entry_px
                        - 1
                    )
                    labels.append(
                        {
                            "trade_date": day,
                            "symbol": result.symbol,
                            "selection_rank": rank,
                            "variant": variant,
                            "attempt": attempt,
                            "branch": result.branch,
                            "ema_score": result.ema_score,
                            "sector_proxy": evidence.sector_proxy,
                            "stock_return_at_entry": evidence.stock_return,
                            "sector_return_at_entry": evidence.sector_return,
                            "market_return_at_entry": evidence.market_return,
                            "sector_strength_passed": evidence.sector_strength_passed,
                            "risk_fraction": fraction,
                            "risk_budget_usd": risk_budget,
                            "used_risk_usd": used_risk,
                            "shares": shares,
                            "notional_usd": notional,
                            "entry_ts_utc": leg.entry_ts_utc,
                            "entry_px": entry_px,
                            "exit_ts_utc": leg.exit_ts_utc,
                            "exit_px": exit_px,
                            "exit_reason": leg.exit_reason,
                            "return_pct": exit_px / entry_px - 1,
                            "entry_delay_minutes": (
                                leg.entry_ts_utc - session["market_open_utc"]
                            ).total_seconds()
                            / 60,
                            "pre_entry_return": (
                                None if opening_px is None else entry_px / opening_px - 1
                            ),
                            "mfe_before_exit": mfe,
                            "mae_before_exit": mae,
                            "entry_relative_spread": entry_spread,
                            "exit_relative_spread_p95": exit_spread,
                            "commission_usd": commission,
                            "net_pnl": net_pnl,
                            "cost_status": cost_status,
                            "production_eligible": False,
                        }
                    )
    if not labels:
        raise RuntimeError("H30 replay produced zero trades")

    census_frame = pl.DataFrame(census).sort("trade_date", "selection_rank", "symbol")
    label_frame = pl.DataFrame(labels).sort(
        "trade_date", "variant", "selection_rank", "symbol", "attempt"
    )
    optional_parents = tuple(
        item[1].dataset_id for item in (sector_data, nbbo_data) if item is not None
    )
    parents = tuple(sorted(set(episode_parents + bar_parents + optional_parents)))
    census_snapshot, _ = persist_snapshot(
        census_frame,
        root=args.data_root,
        source=CENSUS_SOURCE,
        schema_version="h30_challenger_census.v1",
        checks=(
            _check("non_empty", census_frame.height > 0, census_frame.height, ">0"),
            _check(
                "unique_candidate_day",
                census_frame.select(pl.struct("trade_date", "symbol").n_unique()).item()
                == census_frame.height,
                census_frame.height,
                "one row per candidate day",
            ),
        ),
        parent_snapshot_ids=parents,
    )
    census_snapshot.assert_usable()
    max_daily_risk = _number(
        label_frame.group_by("trade_date", "variant")
        .agg(pl.col("used_risk_usd").sum())
        .get_column("used_risk_usd")
        .max(),
        "max_daily_risk",
    )
    max_daily_notional = _number(
        label_frame.group_by("trade_date", "variant")
        .agg(pl.col("notional_usd").sum())
        .get_column("notional_usd")
        .max(),
        "max_daily_notional",
    )
    nbbo_complete = label_frame.filter(
        pl.col("cost_status") != "observed_nbbo_spread_no_market_impact"
    ).is_empty()
    label_snapshot, _ = persist_snapshot(
        label_frame,
        root=args.data_root,
        source=LABEL_SOURCE,
        schema_version="h30_challenger_labels.v1",
        checks=(
            _check("non_empty", label_frame.height > 0, label_frame.height, ">0"),
            _check(
                "daily_risk_cap",
                max_daily_risk <= DAILY_RISK_LIMIT_USD,
                max_daily_risk,
                f"<={DAILY_RISK_LIMIT_USD}",
            ),
            _check(
                "daily_notional_cap",
                max_daily_notional <= PORTFOLIO_NOTIONAL_LIMIT_USD,
                max_daily_notional,
                f"<={PORTFOLIO_NOTIONAL_LIMIT_USD}",
            ),
            _check(
                "nbbo_spread_complete",
                nbbo_complete,
                nbbo_complete,
                "true",
                severity=QualitySeverity.WARNING,
            ),
            _check(
                "market_impact_complete",
                False,
                "historical order-size impact unavailable",
                "required before Paper promotion",
                severity=QualitySeverity.WARNING,
            ),
            _check(
                "research_only",
                label_frame.get_column("production_eligible").not_().all(),
                False,
                "production_eligible=false",
            ),
        ),
        parent_snapshot_ids=(census_snapshot.dataset_id,),
    )
    label_snapshot.assert_usable()
    metric_rows: list[dict[str, object]] = [
        _daily_metrics(label_frame, variant)
        for variant in VARIANTS
    ]
    metric_rows.extend(_fold_metrics(label_frame))
    metric_frame = pl.DataFrame(metric_rows, infer_schema_length=None)
    code_hash = hashlib.sha256(
        (
            sha256_file(ROOT / "research" / "h30_challenger.py")
            + sha256_file(Path(__file__))
        ).encode()
    ).hexdigest()
    metric_frame = metric_frame.with_columns(
        pl.lit(code_hash).alias("code_sha256"),
        pl.lit(5).alias("attempted_configurations"),
        pl.lit(False).alias("production_eligible"),
    )
    observed_fold_count = (
        metric_frame.filter(pl.col("fold").is_not_null()).get_column("fold").n_unique()
    )
    metric_snapshot, metric_path = persist_snapshot(
        metric_frame,
        root=args.data_root,
        source=METRIC_SOURCE,
        schema_version="h30_challenger_metrics.v1",
        checks=(
            _check(
                "five_frozen_variants",
                metric_frame.get_column("variant").n_unique() == 5,
                metric_frame.get_column("variant").n_unique(),
                "5",
            ),
            _check(
                "five_oos_folds",
                metric_frame.filter(pl.col("fold").is_not_null())
                .get_column("fold")
                .n_unique()
                == 5,
                observed_fold_count,
                "5",
            ),
            _check("research_only", True, False, "production_eligible=false"),
        ),
        parent_snapshot_ids=(label_snapshot.dataset_id,),
    )
    metric_snapshot.assert_usable()
    aggregate_rows = {str(row["variant"]): row for row in metric_rows[: len(VARIANTS)]}
    baseline = aggregate_rows["baseline_no_ema"]
    fold_rows = metric_rows[len(VARIANTS) :]
    fold_pnl: dict[tuple[int, str], float] = {
        (int(_number(row["fold"], "fold")), str(row["variant"])): _number(
            row["net_pnl"], "fold_net_pnl"
        )
        for row in fold_rows
    }
    decision_rows: list[dict[str, object]] = []
    for challenger_name in VARIANTS[1:]:
        challenger = aggregate_rows[challenger_name]
        fold_wins = sum(
            fold_pnl[(fold, challenger_name)]
            > fold_pnl[(fold, "baseline_no_ema")]
            for fold in range(1, 6)
        )
        decision = assess_h30_challenger(
            baseline_net_pnl=_number(baseline["net_pnl"], "baseline_net_pnl"),
            challenger_net_pnl=_number(challenger["net_pnl"], "challenger_net_pnl"),
            challenger_profit_factor=(
                _number(challenger["profit_factor"], "challenger_profit_factor")
                if challenger["profit_factor"] is not None
                else None
            ),
            baseline_max_drawdown=_number(
                baseline["max_drawdown_usd"], "baseline_max_drawdown"
            ),
            challenger_max_drawdown=_number(
                challenger["max_drawdown_usd"], "challenger_max_drawdown"
            ),
            trade_legs=int(_number(challenger["trade_legs"], "trade_legs")),
            fold_wins=fold_wins,
            nbbo_cost_complete=nbbo_complete,
        )
        decision_rows.append(
            {
                "challenger": challenger_name,
                "status": decision.status,
                "reasons": list(decision.reasons),
                "fold_wins": fold_wins,
                "attempted_configurations": 5,
                "production_eligible": decision.production_eligible,
            }
        )
    decision_frame = pl.DataFrame(decision_rows)
    decision_snapshot, decision_path = persist_snapshot(
        decision_frame,
        root=args.data_root,
        source=DECISION_SOURCE,
        schema_version="h30_challenger_decision.v1",
        checks=(
            _check(
                "production_ineligible",
                decision_frame.get_column("production_eligible").not_().all(),
                False,
                "false",
            ),
            _check("attempt_budget", True, 5, "5 frozen variants"),
        ),
        parent_snapshot_ids=(metric_snapshot.dataset_id,),
    )
    decision_snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "candidates": census_frame.height,
                "blocked": census_frame.filter(pl.col("status") == "blocked").height,
                "traded_candidates": census_frame.filter(pl.col("status") == "traded").height,
                "metrics": metric_rows[: len(VARIANTS)],
                "dataset_id": metric_snapshot.dataset_id,
                "path": str(metric_path),
                "decisions": decision_rows,
                "decision_path": str(decision_path),
                "production_eligible": False,
            },
            ensure_ascii=False,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
