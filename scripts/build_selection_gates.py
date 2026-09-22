from __future__ import annotations

import argparse
import json
import math
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.candidate_pools import load_premarket_pool
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.providers.massive import fetch_free_float
from data_plane.providers.nasdaq_events import (
    fetch_earnings_calendar,
    fetch_trade_halts,
)
from data_plane.snapshot_queries import load_snapshot_by_id
from data_plane.storage import persist_snapshot
from kernel.config import load_config
from kernel.universe import apply_selection_gates
from operations.local_env import load_project_env

ROOT = Path(__file__).resolve().parents[1]
SIP_MARKET_CAP_SOURCE = "event.sip_market_cap"
SIP_MARKET_CAP_MAX_AGE_SECONDS = 600


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


def _load_target_snapshot(
    data_root: Path,
    *,
    pattern: str,
    date_column: str,
    target_date: date,
) -> tuple[pl.DataFrame, DatasetSnapshot] | None:
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(pattern):
        frame = pl.read_parquet(path, columns=[date_column])
        values = frame.get_column(date_column).unique().to_list()
        if values != [target_date]:
            continue
        snapshot = DatasetSnapshot.model_validate(_manifest(path.parent / "manifest.json"))
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        return None
    _, path, snapshot = max(matches)
    return pl.read_parquet(path), snapshot


def _load_locked(
    data_root: Path, target_date: date
) -> tuple[pl.DataFrame, DatasetSnapshot]:
    loaded = _load_target_snapshot(
        data_root,
        pattern="kernel.catalysts.overnight_candidates-*/data.parquet",
        date_column="session_date",
        target_date=target_date,
    )
    if loaded is None:
        raise FileNotFoundError(f"no catalyst lock for {target_date}")
    return loaded


def _load_daily(
    data_root: Path, previous_session: date
) -> tuple[pl.DataFrame, DatasetSnapshot]:
    loaded = _load_target_snapshot(
        data_root,
        pattern="kernel.universe.daily_precheck-*/data.parquet",
        date_column="asof_date",
        target_date=previous_session,
    )
    if loaded is None:
        raise FileNotFoundError(f"no daily universe for {previous_session}")
    return loaded


def _load_rvol(
    data_root: Path, target_date: date
) -> tuple[pl.DataFrame, DatasetSnapshot] | None:
    return _load_target_snapshot(
        data_root,
        pattern="kernel.premarket.rvol_candidates-*/data.parquet",
        date_column="session_date",
        target_date=target_date,
    )


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


def _store_reference(
    frame: pl.DataFrame,
    *,
    data_root: Path,
    source: str,
    schema_version: str,
    symbols: tuple[str, ...] | None = None,
    target_date: date | None = None,
    date_column: str | None = None,
    key_columns: tuple[str, ...] = ("symbol",),
    parent_ids: tuple[str, ...] = (),
    allow_empty: bool = False,
) -> DatasetSnapshot:
    provenance = f"{source}@{datetime.now(UTC).isoformat()}"
    duplicate_keys = (
        frame.height - frame.select(pl.struct(*key_columns).n_unique()).item()
        if frame.height
        else frame.height - frame.get_column("symbol").n_unique()
    )
    checks = [
        _check(
            "non_empty",
            QualitySeverity.CRITICAL,
            allow_empty or frame.height > 0,
            frame.height,
            "empty is a valid provider result" if allow_empty else "row_count > 0",
            provenance,
        ),
        _check(
            "unique_keys",
            QualitySeverity.CRITICAL,
            duplicate_keys == 0,
            duplicate_keys,
            "0 duplicate provider keys",
            provenance,
        ),
    ]
    if symbols is not None:
        actual = set(frame.get_column("symbol").to_list())
        missing = sorted(set(symbols) - actual)
        checks.append(
            _check(
                "requested_symbol_coverage",
                QualitySeverity.WARNING,
                not missing,
                len(missing),
                f"all {len(symbols)} requested symbols have provider records",
                provenance,
            )
        )
    if target_date is not None and date_column is not None:
        wrong = frame.filter(pl.col(date_column) != target_date).height
        checks.append(
            _check(
                "point_in_time_date",
                QualitySeverity.CRITICAL,
                wrong == 0,
                wrong,
                target_date.isoformat(),
                provenance,
            )
        )
    snapshot, _ = persist_snapshot(
        frame,
        root=data_root,
        source=source,
        schema_version=schema_version,
        checks=tuple(checks),
        parent_snapshot_ids=parent_ids,
    )
    snapshot.assert_usable()
    return snapshot


def _latest_source(
    data_root: Path,
    *,
    source: str,
    predicate: Callable[[pl.DataFrame], bool],
) -> tuple[pl.DataFrame, DatasetSnapshot] | None:
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(f"{source}-*/data.parquet"):
        frame = pl.read_parquet(path)
        if not predicate(frame):
            continue
        snapshot = DatasetSnapshot.model_validate(_manifest(path.parent / "manifest.json"))
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        return None
    _, path, snapshot = max(matches)
    return pl.read_parquet(path), snapshot


def _empty_market_details() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "symbol": pl.String,
            "market_cap": pl.Float64,
            "asof_date": pl.Date,
            "provenance": pl.String,
        }
    )


def _market_details_from_sip_frames(
    frames: list[pl.DataFrame],
    *,
    symbols: tuple[str, ...],
    decision_at: datetime,
) -> pl.DataFrame:
    """Resolve point-in-time market caps from cached-shares × fresh SIP trades.

    This is deliberately the sole live selection source for market capitalisation:
    provider market-cap fallbacks are not equivalent to a contemporaneous SIP price.
    """
    requested = {symbol.upper() for symbol in symbols}
    latest: dict[str, dict[str, object]] = {}
    latest_available_at: dict[str, datetime] = {}
    required = {
        "symbol",
        "market_cap",
        "asof_date",
        "available_at",
        "provenance",
        "market_cap_status",
        "source",
        "price_source",
        "price_timestamp",
    }
    for frame in frames:
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"SIP market-cap snapshot missing columns: {sorted(missing)}")
        for row in frame.iter_rows(named=True):
            symbol = str(row["symbol"]).strip().upper()
            available_at = row["available_at"]
            price_timestamp = row["price_timestamp"]
            cap = row["market_cap"]
            if (
                symbol not in requested
                or row["market_cap_status"] != "available"
                or row["source"] != "derived.sip_market_cap"
                or not isinstance(available_at, datetime)
                or not isinstance(price_timestamp, datetime)
                or available_at.tzinfo is None
                or price_timestamp.tzinfo is None
                or available_at > decision_at
                or price_timestamp > decision_at
                or (decision_at - price_timestamp).total_seconds()
                > SIP_MARKET_CAP_MAX_AGE_SECONDS
                or not isinstance(cap, (int, float))
                or not math.isfinite(float(cap))
                or float(cap) <= 0
                or "alpaca.sip" not in str(row["price_source"])
            ):
                continue
            previous_available_at = latest_available_at.get(symbol)
            if previous_available_at is None or available_at > previous_available_at:
                latest[symbol] = {
                    "symbol": symbol,
                    "market_cap": float(cap),
                    "asof_date": row["asof_date"],
                    "provenance": row["provenance"],
                    "available_at": available_at,
                }
                latest_available_at[symbol] = available_at
    if not latest:
        return _empty_market_details()
    return pl.DataFrame(list(latest.values())).select(
        "symbol", "market_cap", "asof_date", "provenance"
    )


def _load_current_sip_market_caps(
    data_root: Path,
    *,
    symbols: tuple[str, ...],
    decision_at: datetime,
) -> tuple[pl.DataFrame, tuple[str, ...]]:
    frames: list[pl.DataFrame] = []
    snapshot_ids: list[str] = []
    for path in (data_root / "accepted").glob(f"{SIP_MARKET_CAP_SOURCE}-*/data.parquet"):
        snapshot = DatasetSnapshot.model_validate(_manifest(path.parent / "manifest.json"))
        if snapshot.asof_utc > decision_at:
            continue
        frames.append(pl.read_parquet(path))
        snapshot_ids.append(snapshot.dataset_id)
    details = _market_details_from_sip_frames(
        frames,
        symbols=symbols,
        decision_at=decision_at,
    )
    return details, tuple(snapshot_ids)


def _counts(frame: pl.DataFrame, column: str) -> dict[str, int]:
    return {
        str(row[column]): int(row["len"])
        for row in frame.group_by(column).len().sort(column).iter_rows(named=True)
    }


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_parse_date, required=True)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--refresh-events", action="store_true")
    parser.add_argument("--candidate-snapshot")
    parser.add_argument("--rvol-snapshot")
    parser.add_argument("--market-cap-snapshot")
    args = parser.parse_args()
    explicit = (args.candidate_snapshot, args.rvol_snapshot, args.market_cap_snapshot)
    if any(explicit) and not all(explicit):
        raise ValueError("candidate, RVOL and market-cap snapshot IDs must be supplied together")
    rvol_loaded = None
    cap_loaded = None
    if all(explicit):
        pool = load_premarket_pool(
            args.data_root, args.trade_date, pool="catalyst",
            snapshot_id=args.candidate_snapshot,
        )
        candidates, locked_snapshot = pool.frame, pool.snapshot
        rvol_loaded = load_snapshot_by_id(
            args.data_root, args.rvol_snapshot, source="kernel.premarket.rvol_candidates",
            available_by=datetime.now(UTC), required_parents=(locked_snapshot.dataset_id,),
        )
        rvol, _ = rvol_loaded
        if (rvol["session_date"].unique().to_list() != [args.trade_date]
                or rvol.height != candidates.height
                or rvol["symbol"].n_unique() != rvol.height
                or set(rvol["symbol"]) != set(candidates["symbol"])):
            raise ValueError("RVOL wave date or symbol coverage mismatch")
        cutoff = datetime.fromisoformat(next(
            check.expected for check in locked_snapshot.checks if check.name == "wave_context"
        ))
        if ("decision_asof_utc" not in rvol.columns
                or rvol["decision_asof_utc"].unique().to_list() != [cutoff]):
            raise ValueError("RVOL wave cutoff mismatch")
        cap_loaded = load_snapshot_by_id(
            args.data_root, args.market_cap_snapshot, source=SIP_MARKET_CAP_SOURCE,
            available_by=datetime.now(UTC), required_parents=(args.rvol_snapshot,),
        )
    else:
        candidates, locked_snapshot = _load_locked(args.data_root, args.trade_date)

    schedule = build_xnys_schedule(args.trade_date - timedelta(days=15), args.trade_date)
    prior_dates = schedule.filter(pl.col("trade_date") < args.trade_date).get_column(
        "trade_date"
    ).tail(5).to_list()
    if len(prior_dates) != 5:
        raise ValueError("five prior XNYS sessions are required")
    previous_session = prior_dates[-1]
    if all(explicit):
        daily_ids = [
            identity for identity in locked_snapshot.parent_snapshot_ids
            if identity.startswith("kernel.universe.daily_precheck-")
        ]
        if len(daily_ids) != 1:
            raise ValueError("wave candidate must bind one daily universe")
        daily, daily_snapshot = load_snapshot_by_id(
            args.data_root, daily_ids[0], source="kernel.universe.daily_precheck",
            available_by=locked_snapshot.asof_utc,
        )
        if daily["asof_date"].unique().to_list() != [previous_session]:
            raise ValueError("wave daily universe date mismatch")
    else:
        daily, daily_snapshot = _load_daily(args.data_root, previous_session)
    symbols = tuple(candidates.get_column("symbol").sort().to_list())
    symbol_set = set(symbols)

    earnings_cached = None if args.refresh_events else _latest_source(
        args.data_root,
        source="nasdaq.earnings_calendar",
        predicate=lambda frame: frame.get_column("trade_date").unique().to_list()
        == [args.trade_date],
    )
    if earnings_cached is None:
        earnings = fetch_earnings_calendar(args.trade_date)
        earnings_snapshot = _store_reference(
            earnings,
            data_root=args.data_root,
            source="nasdaq.earnings_calendar",
            schema_version="earnings_calendar.v1",
            target_date=args.trade_date,
            date_column="trade_date",
            allow_empty=True,
        )
    else:
        earnings, earnings_snapshot = earnings_cached

    requested_halt_dates = prior_dates + [args.trade_date]
    halts_cached = None if args.refresh_events else _latest_source(
        args.data_root,
        source="nasdaqtrader.trade_halts",
        predicate=lambda frame: set(frame.get_column("halt_date").unique().to_list())
        .intersection(requested_halt_dates)
        == set(requested_halt_dates),
    )
    if halts_cached is None:
        halts = fetch_trade_halts(requested_halt_dates)
        halt_snapshot = _store_reference(
            halts,
            data_root=args.data_root,
            source="nasdaqtrader.trade_halts",
            schema_version="trade_halts.v1",
            key_columns=("symbol", "halt_ts_utc"),
            allow_empty=True,
        )
    else:
        halts, halt_snapshot = halts_cached

    # This endpoint returns only matched records, so a prior subset cannot prove that
    # it was requested for the same locked pool. It is a paginated table query rather
    # than one request per symbol; refresh it for every final gate build.
    provider_date = datetime.now(UTC).date()
    float_cached = _latest_source(
        args.data_root,
        source="massive.free_float",
        predicate=lambda frame: (
            not frame.is_empty()
            and frame.height == frame.get_column("symbol").n_unique()
            and set(frame.get_column("symbol").to_list()).issubset(symbol_set)
            and frame.get_column("retrieved_utc").dt.date().max() == provider_date
        ),
    )
    if float_cached is None:
        floats = fetch_free_float(symbols)
        float_snapshot = _store_reference(
            floats,
            data_root=args.data_root,
            source="massive.free_float",
            schema_version="free_float.v1",
            symbols=symbols,
            parent_ids=(locked_snapshot.dataset_id,),
            allow_empty=True,
        )
    else:
        floats, float_snapshot = float_cached

    if not all(explicit):
        rvol_loaded = _load_rvol(args.data_root, args.trade_date)
    if rvol_loaded is None:
        result = {
            "trade_date": args.trade_date.isoformat(),
            "status": "reference_gates_ready_rvol_pending",
            "locked_symbols": len(symbols),
            "earnings_rows": earnings.height,
            "locked_earnings_symbols": len(symbol_set.intersection(earnings["symbol"])),
            "halt_rows": halts.height,
            "locked_market_caps": 0,
            "locked_free_float": floats.height,
            "selection_snapshot": None,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    rvol_frame, rvol_snapshot = rvol_loaded
    cfg = load_config(ROOT / "config.yaml")
    gate_asof_utc = datetime.now(UTC)
    # Fetching already happens once for the locked pool.  Keep all resulting
    # SIP-derived caps in the audit output; otherwise an unrelated RVOL failure
    # is incorrectly reported as a missing market-cap failure.
    market_snapshot_ids: tuple[str, ...]
    if cap_loaded is not None:
        cap_frame, cap_snapshot = cap_loaded
        market = _market_details_from_sip_frames(
            [cap_frame], symbols=symbols, decision_at=gate_asof_utc,
        )
        market_snapshot_ids = (cap_snapshot.dataset_id,)
    else:
        market, market_snapshot_ids = _load_current_sip_market_caps(
            args.data_root, symbols=symbols, decision_at=gate_asof_utc,
        )

    output = apply_selection_gates(
        daily,
        candidates,
        rvol_frame,
        market,
        earnings,
        halts,
        floats,
        trade_date=args.trade_date,
        asof_utc=gate_asof_utc,
        recent_session_dates=prior_dates,
        cfg=cfg,
        low_float_shares=cfg.universe.luld_low_float_shares,
    )
    actual = set(output.get_column("symbol").to_list())
    duplicate_count = output.height - output.get_column("symbol").n_unique()
    invalid_pass = output.filter(
        pl.col("pass_gate")
        & (
            (pl.col("rvol") <= cfg.universe.min_rvol)
            | ~pl.col("directional_volume_confirmed")
            | pl.col("earnings_day")
            | pl.col("current_halt")
            | pl.col("luld_risk")
            | pl.col("market_cap").is_null()
            | (pl.col("market_cap") < cfg.universe.min_market_cap_usd)
        )
    ).height
    checks = (
        _check(
            "exact_locked_pool",
            QualitySeverity.CRITICAL,
            actual == symbol_set,
            len(actual),
            f"exactly {len(symbols)} symbols",
            "kernel.universe.apply_selection_gates",
        ),
        _check(
            "unique_symbol",
            QualitySeverity.CRITICAL,
            duplicate_count == 0,
            duplicate_count,
            "0 duplicate symbols",
            "kernel.universe.apply_selection_gates",
        ),
        _check(
            "hard_gates_enforced",
            QualitySeverity.CRITICAL,
            invalid_pass == 0,
            invalid_pass,
            "0 passing rows violating a hard gate",
            "kernel.universe.apply_selection_gates",
        ),
        _check(
            "current_sip_market_cap_coverage",
            QualitySeverity.WARNING,
            market.height == len(symbols),
            market.height,
            (
                f"all {len(symbols)} locked candidates have "
                "fresh cached-shares × SIP caps"
            ),
            SIP_MARKET_CAP_SOURCE,
        ),
    )
    parent_ids = (
        locked_snapshot.dataset_id,
        daily_snapshot.dataset_id,
        rvol_snapshot.dataset_id,
        earnings_snapshot.dataset_id,
        halt_snapshot.dataset_id,
        float_snapshot.dataset_id,
        *market_snapshot_ids,
    )
    snapshot, path = persist_snapshot(
        output,
        root=args.data_root,
        source="kernel.universe.selection_gates",
        schema_version="selection_gates.v2",
        checks=checks,
        parent_snapshot_ids=parent_ids,
    )
    snapshot.assert_usable()
    result = {
        "trade_date": args.trade_date.isoformat(),
        "status": "complete",
        "locked_symbols": len(symbols),
        "passes": output.filter(pl.col("pass_gate")).height,
        "rejections": _counts(
            output.filter(~pl.col("pass_gate")), "reject_reason"
        ),
        "market_cap_source": "cached_shares_times_alpaca_sip_trade",
        "market_cap_coverage": {
            "covered": market.height,
            "requested": len(symbols),
        },
        "dataset_id": snapshot.dataset_id,
        "path": str(path),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
