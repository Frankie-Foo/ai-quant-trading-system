"""Incrementally cache SEC shares outstanding for active U.S. common stocks.

The cache is slow metadata, not a market-cap feed.  Live selection still derives
market cap from this cache multiplied by a fresh Alpaca SIP trade.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from data_plane.http import DownloadError, get_json
from data_plane.providers.massive import fetch_ticker_details
from data_plane.providers.sec_filings import sec_user_agent
from operations.local_env import load_project_env, project_data_root
from scripts.build_sec_derived_market_caps import _shares_asof

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "sec.companyfacts.shares_outstanding"
REFERENCE_SOURCE = "massive.reference_tickers.cs"
DEFAULT_MAX_CIKS = 100
MIN_PACE_SECONDS = 0.25


def _empty_cache() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "symbol": pl.String,
            "cik": pl.String,
            "shares_outstanding": pl.Float64,
            "available_at": pl.Datetime("us", "UTC"),
            "source": pl.String,
            "provenance": pl.String,
            "retrieved_at": pl.Datetime("us", "UTC"),
            "fact_filed_date": pl.Date,
            "fact_end_date": pl.Date,
            "fact_tag": pl.String,
        }
    )


def reference_targets(reference: pl.DataFrame) -> pl.DataFrame:
    """Retain only active common stocks whose CIK is known by the reference master."""
    required = {"symbol", "cik", "security_type", "active"}
    missing = required - set(reference.columns)
    if missing:
        raise ValueError(f"reference snapshot missing columns: {sorted(missing)}")
    return (
        reference.filter(
            pl.col("active").eq(True) & pl.col("security_type").cast(pl.String).eq("CS")
        )
        .select(
            pl.col("symbol").cast(pl.String).str.strip_chars().str.to_uppercase().alias("symbol"),
            pl.col("cik")
            .cast(pl.String)
            .str.replace_all(r"\\D", "")
            .str.zfill(10)
            .alias("cik"),
        )
        .filter((pl.col("symbol") != "") & (pl.col("cik") != ""))
        .unique(subset=["symbol"], keep="last")
        .sort("symbol")
    )


def build_cache_rows(
    targets: pl.DataFrame,
    facts_by_cik: Mapping[str, dict[str, Any]],
    *,
    retrieved_at: datetime,
) -> pl.DataFrame:
    """Build rows only for SEC facts with a positive, currently public share count."""
    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise ValueError("retrieved_at must be timezone-aware")
    rows: list[dict[str, object]] = []
    for cik in sorted(facts_by_cik):
        fact = _shares_asof(facts_by_cik[cik], retrieved_at.date())
        if fact is None:
            continue
        shares, filed, end, tag = fact
        for symbol in (
            targets.filter(pl.col("cik") == cik).get_column("symbol").cast(pl.String).to_list()
        ):
            rows.append(
                {
                    "symbol": symbol,
                    "cik": cik,
                    "shares_outstanding": shares,
                    # Company Facts has filing dates but not an intraday public timestamp.
                    # Retrieval time is the only exact time the live cache can claim knowledge.
                    "available_at": retrieved_at.astimezone(UTC),
                    "source": SOURCE,
                    "provenance": f"sec.companyfacts:CIK{cik}:{tag}@filed={filed.isoformat()}",
                    "retrieved_at": retrieved_at.astimezone(UTC),
                    "fact_filed_date": filed,
                    "fact_end_date": end,
                    "fact_tag": tag,
                }
            )
    return pl.DataFrame(rows, schema=_empty_cache().schema) if rows else _empty_cache()


def build_massive_cache_rows(
    targets: pl.DataFrame,
    details: pl.DataFrame,
    *,
    retrieved_at: datetime,
) -> pl.DataFrame:
    """Use provider-reported weighted shares only when SEC has no usable fact."""
    target_ciks = {
        str(row["symbol"]): str(row["cik"])
        for row in targets.select("symbol", "cik").iter_rows(named=True)
    }
    rows: list[dict[str, object]] = []
    for row in details.iter_rows(named=True):
        symbol = str(row["symbol"]).strip().upper()
        shares = row.get("weighted_shares_outstanding")
        if not isinstance(shares, (int, float)) or shares <= 0 or symbol not in target_ciks:
            continue
        rows.append(
            {
                "symbol": symbol,
                "cik": target_ciks[symbol],
                "shares_outstanding": float(shares),
                "available_at": retrieved_at.astimezone(UTC),
                "source": "massive.ticker_details.weighted_shares_outstanding",
                "provenance": str(row["provenance"]),
                "retrieved_at": retrieved_at.astimezone(UTC),
                "fact_filed_date": None,
                "fact_end_date": None,
                "fact_tag": "weighted_shares_outstanding",
            }
        )
    return pl.DataFrame(rows, schema=_empty_cache().schema) if rows else _empty_cache()


def merge_share_cache(
    existing: pl.DataFrame,
    refreshed: pl.DataFrame,
    *,
    refreshed_ciks: set[str],
) -> pl.DataFrame:
    """Atomically replace known rows only after a CIK produced a usable fact."""
    if not refreshed_ciks:
        return existing.sort("symbol") if existing.height else _empty_cache()
    if "cik" not in existing.columns:
        raise ValueError("existing shares cache missing cik")
    retained = existing.filter(~pl.col("cik").cast(pl.String).is_in(sorted(refreshed_ciks)))
    return pl.concat((retained, refreshed), how="diagonal_relaxed").sort("symbol")


def _latest_reference_path(data_root: Path) -> Path:
    matches: list[tuple[datetime, Path]] = []
    for path in (data_root / "accepted").glob(f"{REFERENCE_SOURCE}-*/data.parquet"):
        manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
        value = datetime.fromisoformat(str(manifest["asof_utc"]).replace("Z", "+00:00"))
        matches.append((value.astimezone(UTC), path))
    if not matches:
        raise FileNotFoundError("no accepted active-common-stock reference snapshot")
    return max(matches)[1]


def _read_cache(path: Path) -> pl.DataFrame:
    if not path.is_file():
        return _empty_cache()
    frame = pl.read_parquet(path)
    required = {"symbol", "cik", "shares_outstanding", "available_at", "source", "provenance"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"shares cache missing columns: {sorted(missing)}")
    return frame


def _write_cache(path: Path, frame: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    frame.write_parquet(temp, compression="zstd", statistics=True)
    temp.replace(path)


def _state_connection(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sec_share_cache_state (
            cik TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            retry_after TEXT,
            last_error TEXT
        )
        """
    )
    return connection


def _state_rows(connection: sqlite3.Connection) -> dict[str, dict[str, object]]:
    rows = connection.execute(
        "SELECT cik, status, attempts, updated_at, retry_after FROM sec_share_cache_state"
    ).fetchall()
    return {
        str(cik): {
            "status": str(status),
            "attempts": int(attempts),
            "updated_at": datetime.fromisoformat(str(updated_at)),
            "retry_after": datetime.fromisoformat(str(retry_after)) if retry_after else None,
        }
        for cik, status, attempts, updated_at, retry_after in rows
    }


def _upsert_state(
    connection: sqlite3.Connection,
    *,
    cik: str,
    status: str,
    now: datetime,
    retry_after: datetime | None,
    last_error: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO sec_share_cache_state(
            cik, status, attempts, updated_at, retry_after, last_error
        )
        VALUES (?, ?, 1, ?, ?, ?)
        ON CONFLICT(cik) DO UPDATE SET
            status=excluded.status,
            attempts=sec_share_cache_state.attempts + 1,
            updated_at=excluded.updated_at,
            retry_after=excluded.retry_after,
            last_error=excluded.last_error
        """,
        (
            cik,
            status,
            now.isoformat(),
            retry_after.isoformat() if retry_after else None,
            last_error,
        ),
    )


def _cached_symbols_by_cik(cache: pl.DataFrame) -> dict[str, set[str]]:
    output: dict[str, set[str]] = {}
    if cache.is_empty() or "cik" not in cache.columns:
        return output
    for row in cache.select("symbol", "cik").iter_rows(named=True):
        output.setdefault(str(row["cik"]), set()).add(str(row["symbol"]))
    return output


def _pending_ciks(
    targets: pl.DataFrame,
    cache: pl.DataFrame,
    state: Mapping[str, Mapping[str, object]],
    *,
    now: datetime,
    refresh_after: timedelta,
) -> tuple[str, ...]:
    cached_symbols = _cached_symbols_by_cik(cache)
    grouped: dict[str, set[str]] = {}
    for row in targets.iter_rows(named=True):
        grouped.setdefault(str(row["cik"]), set()).add(str(row["symbol"]))
    pending: list[str] = []
    for cik, symbols in sorted(grouped.items()):
        record = state.get(cik)
        complete = symbols.issubset(cached_symbols.get(cik, set()))
        if record is None:
            pending.append(cik)
            continue
        updated_at = record["updated_at"]
        retry_after = record["retry_after"]
        if not isinstance(updated_at, datetime):
            pending.append(cik)
        elif record["status"] == "available" and complete and now - updated_at < refresh_after:
            continue
        elif isinstance(retry_after, datetime) and retry_after > now:
            continue
        else:
            pending.append(cik)
    return tuple(pending)


def _headers() -> dict[str, str]:
    return {"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"}


def _cache_path(data_root: Path, configured: Path | None) -> Path:
    if configured is not None:
        return configured
    value = os.getenv("AI_QUANT_SHARES_CACHE_FILE", "").strip()
    return Path(value) if value else data_root / "cache" / "sec-shares-outstanding.parquet"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    parser.add_argument("--reference-snapshot", type=Path)
    parser.add_argument("--cache-file", type=Path)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--max-ciks", type=int, default=DEFAULT_MAX_CIKS)
    parser.add_argument("--pace-seconds", type=float, default=MIN_PACE_SECONDS)
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--refresh-days", type=int, default=14)
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--massive-fallback", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.max_ciks <= 0:
        raise ValueError("max-ciks must be positive")
    if args.pace_seconds < MIN_PACE_SECONDS:
        raise ValueError(f"pace-seconds must be at least {MIN_PACE_SECONDS}")
    if args.attempts <= 0:
        raise ValueError("attempts must be positive")
    if args.timeout_seconds <= 0:
        raise ValueError("timeout-seconds must be positive")
    if args.refresh_days <= 0:
        raise ValueError("refresh-days must be positive")
    load_project_env(ROOT)
    now = datetime.now(UTC)
    reference_path = args.reference_snapshot or _latest_reference_path(args.data_root)
    targets = reference_targets(pl.read_parquet(reference_path))
    if args.symbols:
        requested_symbols = {symbol.strip().upper() for symbol in args.symbols if symbol.strip()}
        targets = targets.filter(pl.col("symbol").is_in(sorted(requested_symbols)))
        missing_symbols = requested_symbols - set(targets.get_column("symbol").to_list())
        if missing_symbols:
            raise ValueError(
                f"symbols absent from active common-stock reference: {sorted(missing_symbols)}"
            )
    cache_path = _cache_path(args.data_root, args.cache_file)
    state_path = args.state_file or args.data_root / "state" / "sec-shares-cache.sqlite3"
    cache = _read_cache(cache_path)
    with _state_connection(state_path) as connection:
        pending = (
            tuple(sorted(set(targets.get_column("cik").to_list())))
            if args.force
            else _pending_ciks(
                targets,
                cache,
                _state_rows(connection),
                now=now,
                refresh_after=timedelta(days=args.refresh_days),
            )
        )
        selected = pending[: args.max_ciks]
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "status": "dry_run",
                        "reference": str(reference_path),
                        "cache": str(cache_path),
                        "active_symbols": targets.height,
                        "pending_ciks": len(pending),
                        "selected_ciks": list(selected),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        headers = _headers()
        facts: dict[str, dict[str, Any]] = {}
        missing_ciks: list[str] = []
        missing = 0
        failed = 0
        for index, cik in enumerate(selected):
            if index:
                time.sleep(args.pace_seconds)
            try:
                payload = get_json(
                    f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
                    headers=headers,
                    attempts=args.attempts,
                    timeout_seconds=args.timeout_seconds,
                )
                if _shares_asof(payload, now.date()) is None:
                    missing += 1
                    missing_ciks.append(cik)
                    _upsert_state(
                        connection,
                        cik=cik,
                        status="missing_fact",
                        now=now,
                        retry_after=now + timedelta(days=7),
                        last_error=None,
                    )
                else:
                    facts[cik] = payload
            except DownloadError as exc:
                failed += 1
                _upsert_state(
                    connection,
                    cik=cik,
                    status="download_error",
                    now=now,
                    retry_after=now + timedelta(hours=1),
                    last_error=str(exc),
                )
        provider_rows = _empty_cache()
        if args.massive_fallback and missing_ciks:
            fallback_targets = targets.filter(pl.col("cik").is_in(missing_ciks))
            details = fetch_ticker_details(
                tuple(fallback_targets.get_column("symbol").to_list()),
                now.date(),
                pace_seconds=0,
            )
            provider_rows = build_massive_cache_rows(fallback_targets, details, retrieved_at=now)
        refreshed = pl.concat(
            (build_cache_rows(targets, facts, retrieved_at=now), provider_rows),
            how="diagonal_relaxed",
        )
        refreshed_ciks = set(facts) | set(provider_rows.get_column("cik").to_list())
        if refreshed_ciks:
            _write_cache(
                cache_path,
                merge_share_cache(cache, refreshed, refreshed_ciks=refreshed_ciks),
            )
            for cik in refreshed_ciks:
                _upsert_state(
                    connection,
                    cik=cik,
                    status="available",
                    now=now,
                    retry_after=None,
                    last_error=None,
                )
        connection.commit()
    print(
        json.dumps(
            {
                "status": "complete",
                "reference": str(reference_path),
                "cache": str(cache_path),
                "active_symbols": targets.height,
                "pending_ciks": len(pending),
                "requested_ciks": len(selected),
                "available_ciks": len(facts),
                "missing_facts": missing,
                "provider_fallback_rows": provider_rows.height,
                "download_errors": failed,
                "cached_rows": _read_cache(cache_path).height,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
