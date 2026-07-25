from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl
from dotenv import load_dotenv

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import (
    DataQualityCheck,
    DatasetSnapshot,
    QualitySeverity,
)
from data_plane.providers.alpaca import AlpacaStockFeed, fetch_bars, stock_data_policy_from_env
from data_plane.quality import BAR_SCHEMA_VERSION, audit_minute_bars
from data_plane.storage import persist_snapshot
from kernel.config import load_config
from kernel.features.momentum import premarket_window_utc, rvol

ROOT = Path(__file__).resolve().parents[1]
BEIJING = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")
HISTORY_SESSIONS = 20
RAW_SOURCE = "alpaca.sip.premarket_1m"
FEATURE_SOURCE = "kernel.premarket.rvol_candidates"


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"manifest is not an object: {path}")
    return value


def _load_locked_candidates(
    data_root: Path, target_date: date
) -> tuple[pl.DataFrame, DatasetSnapshot]:
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(
        "kernel.catalysts.overnight_candidates-*/data.parquet"
    ):
        frame = pl.read_parquet(path, columns=["session_date"])
        dates = frame.get_column("session_date").unique().to_list()
        if dates != [target_date]:
            continue
        snapshot = DatasetSnapshot.model_validate(_manifest(path.parent / "manifest.json"))
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        raise FileNotFoundError(f"no locked catalyst snapshot for {target_date}")
    _, path, snapshot = max(matches)
    candidates = pl.read_parquet(path).sort("symbol")
    if candidates.get_column("symbol").n_unique() != candidates.height:
        raise ValueError("locked catalyst snapshot contains duplicate symbols")
    return candidates, snapshot


def _check(
    name: str,
    severity: QualitySeverity,
    passed: bool,
    observed: object,
    expected: str,
    provenance: str,
) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=severity,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance=provenance,
    )


def _symbol_hash(symbols: tuple[str, ...]) -> str:
    return hashlib.sha256(",".join(symbols).encode("utf-8")).hexdigest()[:16]


def _query_provenance(
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
    *,
    feed: AlpacaStockFeed,
) -> str:
    return (
        f"{RAW_SOURCE}@{start_utc.isoformat()}..{end_utc.isoformat()}|"
        f"symbols_sha256={_symbol_hash(symbols)}|feed={feed}|adjustment=split"
    )


def _load_cached_session(
    data_root: Path,
    *,
    query_provenance: str,
) -> tuple[pl.DataFrame, DatasetSnapshot] | None:
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(f"{RAW_SOURCE}-*/data.parquet"):
        snapshot = DatasetSnapshot.model_validate(_manifest(path.parent / "manifest.json"))
        if any(check.provenance == query_provenance for check in snapshot.checks):
            matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        return None
    _, path, snapshot = max(matches)
    return pl.read_parquet(path), snapshot


def _raw_checks(
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
    returned_symbols = set(frame.get_column("symbol").unique().to_list())
    outside = sorted(returned_symbols - set(symbols))
    outside_window = frame.filter(
        (pl.col("ts_utc") < start_utc) | (pl.col("ts_utc") >= end_utc)
    ).height
    missing_no_bar = sorted(set(symbols) - returned_symbols)
    checks.extend(
        [
            _check(
                "provider_query_completed",
                QualitySeverity.CRITICAL,
                True,
                True,
                "authenticated request and all pagination completed",
                provenance,
            ),
            _check(
                "requested_symbol_scope",
                QualitySeverity.CRITICAL,
                not outside,
                outside or "complete",
                "no returned symbols outside the locked pool",
                provenance,
            ),
            _check(
                "half_open_query_window",
                QualitySeverity.CRITICAL,
                outside_window == 0,
                outside_window,
                f"all timestamps in [{start_utc.isoformat()}, {end_utc.isoformat()})",
                provenance,
            ),
            _check(
                "symbols_without_emitted_bars",
                QualitySeverity.INFO,
                not missing_no_bar,
                len(missing_no_bar),
                "absence means zero qualifying bar volume, never a forward fill",
                provenance,
            ),
        ]
    )
    return tuple(checks)


def _get_session(
    data_root: Path,
    *,
    symbols: tuple[str, ...],
    trade_date: date,
    cutoff_et: time,
    locked_snapshot_id: str,
    feed: AlpacaStockFeed,
    refresh: bool,
) -> tuple[pl.DataFrame, DatasetSnapshot, bool]:
    start_utc, end_utc = premarket_window_utc(trade_date, cutoff_et)
    provenance = _query_provenance(symbols, start_utc, end_utc, feed=feed)
    if not refresh:
        cached = _load_cached_session(data_root, query_provenance=provenance)
        if cached is not None:
            return cached[0], cached[1], True

    frame = fetch_bars(symbols, start_utc, end_utc, feed=feed)
    checks = _raw_checks(
        frame,
        symbols=symbols,
        start_utc=start_utc,
        end_utc=end_utc,
        provenance=provenance,
    )
    snapshot, _ = persist_snapshot(
        frame,
        root=data_root,
        source=RAW_SOURCE,
        schema_version=BAR_SCHEMA_VERSION,
        checks=checks,
        parent_snapshot_ids=(locked_snapshot_id,),
    )
    snapshot.assert_usable()
    return frame, snapshot, False


def _feature_checks(
    frame: pl.DataFrame,
    *,
    symbols: tuple[str, ...],
    trade_date: date,
    decision_asof_utc: datetime,
    min_rvol: float,
    min_premarket_return: float,
    min_premarket_close_location: float,
) -> tuple[DataQualityCheck, ...]:
    provenance = f"{FEATURE_SOURCE}@{decision_asof_utc.isoformat()}"
    actual = set(frame.get_column("symbol").to_list())
    duplicate_count = frame.height - frame.get_column("symbol").n_unique()
    wrong_dates = frame.filter(pl.col("session_date") != trade_date).height
    future_cutoffs = frame.filter(pl.col("data_cutoff_utc") > decision_asof_utc).height
    invalid_available = frame.filter(
        (pl.col("availability") == "available")
        & (
            pl.col("rvol").is_null()
            | ~pl.col("rvol").is_finite()
            | (pl.col("rvol") < 0)
        )
    ).height
    invalid_pass = frame.filter(
        pl.col("rvol_pass") != (pl.col("rvol") > min_rvol).fill_null(False)
    ).height
    expected_price_confirmation = (
        (pl.col("premarket_return") > min_premarket_return)
        & pl.col("premarket_above_vwap")
        & (
            pl.col("premarket_close_location")
            >= min_premarket_close_location
        )
    ).fill_null(False)
    invalid_price_confirmation = frame.filter(
        pl.col("premarket_price_confirmation")
        != expected_price_confirmation
    ).height
    return (
        _check(
            "exact_locked_pool",
            QualitySeverity.CRITICAL,
            actual == set(symbols),
            f"rows={frame.height}, symbols={len(actual)}",
            f"exactly {len(symbols)} locked symbols",
            provenance,
        ),
        _check(
            "unique_symbol",
            QualitySeverity.CRITICAL,
            duplicate_count == 0,
            duplicate_count,
            "0 duplicate symbols",
            provenance,
        ),
        _check(
            "target_session",
            QualitySeverity.CRITICAL,
            wrong_dates == 0,
            wrong_dates,
            trade_date.isoformat(),
            provenance,
        ),
        _check(
            "no_data_after_decision",
            QualitySeverity.CRITICAL,
            future_cutoffs == 0,
            future_cutoffs,
            "data cutoff is no later than decision asof",
            provenance,
        ),
        _check(
            "finite_available_rvol",
            QualitySeverity.CRITICAL,
            invalid_available == 0,
            invalid_available,
            "available RVOL is finite and nonnegative",
            provenance,
        ),
        _check(
            "strict_rvol_gate",
            QualitySeverity.CRITICAL,
            invalid_pass == 0,
            invalid_pass,
            f"rvol_pass iff RVOL > {min_rvol}",
            provenance,
        ),
        _check(
            "premarket_price_confirmation",
            QualitySeverity.CRITICAL,
            invalid_price_confirmation == 0,
            invalid_price_confirmation,
            (
                f"return > {min_premarket_return}, close > VWAP, and "
                f"close location >= {min_premarket_close_location}"
            ),
            provenance,
        ),
    )


def _counts(frame: pl.DataFrame, column: str) -> dict[str, int]:
    return {
        str(row[column]): int(row["len"])
        for row in frame.group_by(column).len().sort(column).iter_rows(named=True)
    }


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_parse_date, required=True)
    parser.add_argument("--decision-asof", type=_parse_utc)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    cfg = load_config(ROOT / "config.yaml")
    policy = stock_data_policy_from_env()
    decision_time_beijing = datetime.strptime(
        cfg.guardrails.selection_time_beijing, "%H:%M"
    ).time()
    default_decision = datetime.combine(
        args.trade_date, decision_time_beijing, BEIJING
    ).astimezone(UTC)
    decision_asof_utc: datetime = args.decision_asof or default_decision
    if decision_asof_utc.second or decision_asof_utc.microsecond:
        raise ValueError("decision asof must be aligned to an exact minute")
    data_cutoff_utc = decision_asof_utc - timedelta(minutes=policy.delay_minutes)
    cutoff_local = data_cutoff_utc.astimezone(NEW_YORK)
    if cutoff_local.date() != args.trade_date:
        raise ValueError("market-data cutoff must fall on the target New York date")
    cutoff_et = cutoff_local.time().replace(tzinfo=None)
    premarket_window_utc(args.trade_date, cutoff_et)

    candidates, locked_snapshot = _load_locked_candidates(args.data_root, args.trade_date)
    symbols = tuple(candidates.get_column("symbol").to_list())
    schedule = build_xnys_schedule(args.trade_date - timedelta(days=60), args.trade_date)
    session_dates = schedule.get_column("trade_date").tail(HISTORY_SESSIONS + 1).to_list()
    if len(session_dates) != HISTORY_SESSIONS + 1 or session_dates[-1] != args.trade_date:
        raise ValueError("target session plus 20 prior XNYS sessions are unavailable")

    now_utc = datetime.now(UTC)
    target_ready = now_utc >= decision_asof_utc
    requested_dates = session_dates if target_ready else session_dates[:-1]
    frames: list[pl.DataFrame] = []
    raw_snapshots: list[DatasetSnapshot] = []
    cache_hits = 0
    for session_date in requested_dates:
        frame, snapshot, cached = _get_session(
            args.data_root,
            symbols=symbols,
            trade_date=session_date,
            cutoff_et=cutoff_et,
            locked_snapshot_id=locked_snapshot.dataset_id,
            feed=policy.feed,
            refresh=args.refresh,
        )
        frames.append(frame)
        raw_snapshots.append(snapshot)
        cache_hits += int(cached)

    if not target_ready:
        result = {
            "trade_date": args.trade_date.isoformat(),
            "status": "history_ready_target_pending",
            "locked_symbols": len(symbols),
            "history_sessions": len(raw_snapshots),
            "history_rows": sum(frame.height for frame in frames),
            "cache_hits": cache_hits,
            "target_query_not_before_utc": decision_asof_utc.isoformat(),
            "target_query_not_before_beijing": decision_asof_utc.astimezone(
                BEIJING
            ).isoformat(),
            "data_cutoff_utc": data_cutoff_utc.isoformat(),
            "feature_snapshot": None,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    bars = pl.concat(frames) if frames else pl.DataFrame()
    features = rvol(
        bars,
        schedule=schedule,
        target_date=args.trade_date,
        symbols=symbols,
        complete_session_dates=session_dates,
        cutoff_et=cutoff_et,
        n=HISTORY_SESSIONS,
        min_rvol=cfg.universe.min_rvol,
        min_premarket_return=cfg.universe.min_premarket_return,
        min_premarket_close_location=cfg.universe.min_premarket_close_location,
        provenance=(
            f"alpaca.{policy.feed}.split_adjusted[{len(raw_snapshots)}sessions]"
            f"@{data_cutoff_utc.isoformat()}"
        ),
    ).with_columns(
        pl.lit(decision_asof_utc).cast(pl.Datetime("ms", "UTC")).alias(
            "decision_asof_utc"
        ),
        pl.lit(policy.delay_minutes).alias("provider_delay_minutes"),
        pl.lit(policy.feed).alias("market_data_feed"),
        pl.lit(policy.is_realtime).alias("market_data_realtime"),
    )
    output = candidates.join(features, on="symbol", how="left", validate="1:1")
    checks = _feature_checks(
        output,
        symbols=symbols,
        trade_date=args.trade_date,
        decision_asof_utc=decision_asof_utc,
        min_rvol=cfg.universe.min_rvol,
        min_premarket_return=cfg.universe.min_premarket_return,
        min_premarket_close_location=cfg.universe.min_premarket_close_location,
    )
    parent_ids = (locked_snapshot.dataset_id,) + tuple(
        snapshot.dataset_id for snapshot in raw_snapshots
    )
    snapshot, path = persist_snapshot(
        output,
        root=args.data_root,
        source=FEATURE_SOURCE,
        schema_version="premarket_rvol_candidates.v2",
        checks=checks,
        parent_snapshot_ids=parent_ids,
    )
    snapshot.assert_usable()
    result = {
        "trade_date": args.trade_date.isoformat(),
        "status": "complete",
        "locked_symbols": len(symbols),
        "raw_sessions": len(raw_snapshots),
        "raw_rows": sum(frame.height for frame in frames),
        "cache_hits": cache_hits,
        "decision_asof_utc": decision_asof_utc.isoformat(),
        "data_cutoff_utc": data_cutoff_utc.isoformat(),
        "cutoff_et_exclusive": cutoff_et.isoformat(),
        "market_data_feed": policy.feed,
        "market_data_realtime": policy.is_realtime,
        "availability": _counts(output, "availability"),
        "rvol_pass": output.filter(pl.col("rvol_pass")).height,
        "dataset_id": snapshot.dataset_id,
        "path": str(path),
        "failed_critical_checks": [
            check.name
            for check in checks
            if check.severity is QualitySeverity.CRITICAL and not check.passed
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
