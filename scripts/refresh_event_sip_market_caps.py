"""Refresh candidate market caps from cached shares and current Alpaca SIP trades.

This tool never fetches shares or third-party market caps.  The shares cache is
an explicit, user-owned input; market caps are only ``shares × fresh SIP price``.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from data_plane.contracts import DataQualityCheck, QualitySeverity
from data_plane.event_universe import SipMarketCapPolicy, derive_sip_market_caps
from data_plane.providers.alpaca import fetch_quotes, fetch_trades
from data_plane.snapshot_queries import load_snapshot_by_id
from data_plane.storage import persist_snapshot
from operations.local_env import load_project_env, project_data_root, sip_monitoring_window

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "event.sip_market_cap"
RVOL_SOURCE = "kernel.premarket.rvol_candidates"


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _symbols(values: list[str]) -> tuple[str, ...]:
    symbols = tuple(sorted({value.strip().upper() for value in values if value.strip()}))
    if not symbols:
        raise ValueError("at least one symbol is required")
    return symbols


def _rvol_symbols(data_root: Path, trade_date: date) -> tuple[str, ...]:
    """Load only the current frozen RVOL candidate identities.

    This deliberately does not invent a broad symbol universe.  A market-cap
    refresh may cover only names already discovered by the point-in-time
    catalyst/RVOL pipeline.
    """
    matches: list[tuple[datetime, Path]] = []
    for path in (data_root / "accepted").glob(f"{RVOL_SOURCE}-*/data.parquet"):
        frame = pl.read_parquet(path, columns=["session_date"])
        if frame.get_column("session_date").unique().to_list() != [trade_date]:
            continue
        manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
        created = datetime.fromisoformat(str(manifest["asof_utc"]).replace("Z", "+00:00"))
        matches.append((created.astimezone(UTC), path))
    if not matches:
        raise FileNotFoundError(f"no accepted RVOL candidate snapshot for {trade_date.isoformat()}")
    _, path = max(matches)
    frame = pl.read_parquet(path, columns=["symbol"])
    return tuple(
        sorted(
            {
                symbol.strip().upper()
                for symbol in frame.get_column("symbol").cast(pl.String).to_list()
                if symbol.strip()
            }
        )
    )


def _require_columns(frame: pl.DataFrame, *columns: str) -> None:
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f"shares cache missing required columns: {sorted(missing)}")


def build_sip_market_cap_snapshot(
    shares: pl.DataFrame,
    trades: pl.DataFrame,
    *,
    as_of: datetime,
    max_trade_age_seconds: int,
    requested_symbols: tuple[str, ...] | None = None,
    quotes: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Pure join used by the CLI and tests; no network or file writes."""
    frame = derive_sip_market_caps(
        shares,
        trades,
        as_of=as_of,
        policy=SipMarketCapPolicy(max_trade_age_seconds=max_trade_age_seconds),
        quotes=quotes,
    )
    if requested_symbols is None:
        return frame
    existing = {
        symbol.strip().upper()
        for symbol in frame.get_column("symbol").cast(pl.String).to_list()
        if symbol.strip()
    }
    missing = sorted(set(requested_symbols) - existing)
    if not missing:
        return frame
    missing_rows = pl.DataFrame(
        {
            "symbol": missing,
            "asof_date": [as_of.date()] * len(missing),
            "market_cap": [None] * len(missing),
            "available_at": [None] * len(missing),
            "source": ["derived.sip_market_cap"] * len(missing),
            "provenance": ["shares_cache_missing"] * len(missing),
            "market_cap_status": ["shares_unavailable"] * len(missing),
            "shares_source": [None] * len(missing),
            "price_source": [None] * len(missing),
            "price_timestamp": [None] * len(missing),
        },
        schema=_empty_snapshot_frame().schema,
    )
    return pl.concat((frame, missing_rows), how="diagonal_relaxed").sort("symbol")


def _checks(frame: pl.DataFrame, *, requested: int) -> tuple[DataQualityCheck, ...]:
    available = frame.filter(pl.col("market_cap_status") == "available").height
    non_sip = frame.filter(pl.col("price_source").is_not_null()).filter(
        ~pl.col("price_source").cast(pl.String).str.contains("alpaca.sip")
    ).height
    return (
        DataQualityCheck(
            name="requested_symbols_retained",
            severity=QualitySeverity.CRITICAL,
            passed=frame.height == requested,
            observed=str(frame.height),
            expected=str(requested),
            provenance=SOURCE,
        ),
        DataQualityCheck(
            name="sip_price_source_only",
            severity=QualitySeverity.CRITICAL,
            passed=non_sip == 0,
            observed=str(non_sip),
            expected="0",
            provenance=SOURCE,
        ),
        DataQualityCheck(
            name="current_cap_coverage",
            severity=QualitySeverity.WARNING,
            passed=available == requested,
            observed=str(available),
            expected=str(requested),
            provenance=SOURCE,
        ),
    )


def _empty_snapshot_frame() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "symbol": pl.String,
            "asof_date": pl.Date,
            "market_cap": pl.Float64,
            "available_at": pl.Datetime("us", "UTC"),
            "source": pl.String,
            "provenance": pl.String,
            "market_cap_status": pl.String,
            "shares_source": pl.String,
            "price_source": pl.String,
            "price_timestamp": pl.Datetime("us", "UTC"),
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shares-cache", type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--symbols", nargs="+")
    source.add_argument("--trade-date", type=date.fromisoformat)
    parser.add_argument("--as-of-utc", type=_utc)
    parser.add_argument("--max-trade-age-seconds", type=int, default=600)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    parser.add_argument("--rvol-snapshot")
    args = parser.parse_args()
    if args.max_trade_age_seconds < 0:
        raise ValueError("max trade age must be non-negative")
    as_of = args.as_of_utc or datetime.now(UTC)
    if not sip_monitoring_window(as_of):
        raise PermissionError("Alpaca SIP refresh is allowed only during the monitoring window")
    parent_ids: tuple[str, ...] = ()
    if args.rvol_snapshot:
        if args.trade_date is None:
            raise ValueError("explicit RVOL snapshot requires trade date")
        rvol, parent = load_snapshot_by_id(
            args.data_root, args.rvol_snapshot, source=RVOL_SOURCE,
            available_by=datetime.now(UTC),
        )
        if not rvol.is_empty() and rvol["session_date"].unique().to_list() != [args.trade_date]:
            raise ValueError("RVOL trade date mismatch")
        if rvol["symbol"].n_unique() != rvol.height:
            raise ValueError("RVOL contains duplicate symbols")
        symbols = tuple(sorted(rvol["symbol"].to_list()))
        parent_ids = (parent.dataset_id,)
    else:
        symbols = (
            _symbols(args.symbols) if args.symbols
            else _rvol_symbols(args.data_root, args.trade_date)
        )
    if not symbols:
        snapshot, path = persist_snapshot(
            _empty_snapshot_frame(),
            root=args.data_root,
            source=SOURCE,
            schema_version="event_sip_market_cap.v1",
            checks=_checks(_empty_snapshot_frame(), requested=0),
            parent_snapshot_ids=parent_ids,
        )
        snapshot.assert_usable()
        print(
            json.dumps(
                {
                    "status": "complete",
                    "dataset_id": snapshot.dataset_id,
                    "path": str(path),
                    "requested": 0,
                    "covered": 0,
                    "source": "cached_shares_times_alpaca_sip_trade",
                },
                ensure_ascii=False,
            )
        )
        return 0
    load_project_env(ROOT, now_utc=as_of)
    configured_cache = os.getenv("AI_QUANT_SHARES_CACHE_FILE", "").strip()
    shares_cache = args.shares_cache or (
        Path(configured_cache)
        if configured_cache
        else args.data_root / "cache" / "sec-shares-outstanding.parquet"
    )
    if not shares_cache.is_file():
        raise FileNotFoundError("configured shares cache is missing")
    shares = pl.read_parquet(shares_cache)
    _require_columns(
        shares,
        "symbol",
        "shares_outstanding",
        "available_at",
        "source",
        "provenance",
    )
    shares = shares.filter(pl.col("symbol").cast(pl.String).str.to_uppercase().is_in(symbols))
    if shares.is_empty():
        raise RuntimeError("shares cache has no requested symbols")
    trade_frame = fetch_trades(
        tuple(shares.get_column("symbol").cast(pl.String).str.to_uppercase().unique()),
        as_of - timedelta(seconds=max(60, args.max_trade_age_seconds)),
        as_of + timedelta(microseconds=1),
        feed="sip",
    ).with_columns(pl.lit(as_of).cast(pl.Datetime("us", "UTC")).alias("available_at"))
    quote_frame = fetch_quotes(
        tuple(shares.get_column("symbol").cast(pl.String).str.to_uppercase().unique()),
        as_of - timedelta(seconds=max(60, args.max_trade_age_seconds)),
        as_of + timedelta(microseconds=1),
        feed="sip",
    ).with_columns(pl.lit(as_of).cast(pl.Datetime("us", "UTC")).alias("available_at"))
    snapshot_frame = build_sip_market_cap_snapshot(
        shares,
        trade_frame,
        as_of=as_of,
        max_trade_age_seconds=args.max_trade_age_seconds,
        requested_symbols=symbols,
        quotes=quote_frame,
    )
    snapshot, path = persist_snapshot(
        snapshot_frame,
        root=args.data_root,
        source=SOURCE,
        schema_version="event_sip_market_cap.v1",
        checks=_checks(snapshot_frame, requested=len(symbols)),
        parent_snapshot_ids=parent_ids,
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
                "requested": len(symbols),
                "covered": snapshot_frame.filter(
                    pl.col("market_cap_status") == "available"
                ).height,
                "source": "cached_shares_times_alpaca_sip_trade",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
