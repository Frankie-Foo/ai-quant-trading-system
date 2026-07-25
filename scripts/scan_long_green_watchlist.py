"""Scan the locked selection pool for point-in-time long-green expansion profiles."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl
from dotenv import load_dotenv

from data_plane.calendar import build_xnys_schedule
from data_plane.http import DownloadError
from data_plane.providers.alpaca import (
    fetch_sparse_bars_for_monitoring,
    stock_data_policy_from_env,
)
from kernel.technical_monitor import (
    LongGreenExpansion,
    build_long_green_expansion,
    resample_completed_bars,
)

ROOT = Path(__file__).resolve().parents[1]
EASTERN = ZoneInfo("America/New_York")


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("trade-date must use YYYY-MM-DD") from exc


def _utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("asof must include a timezone")
    return parsed.astimezone(UTC)


def _latest_selection(data_root: Path, trade_date: date) -> tuple[Path, pl.DataFrame]:
    matches: list[tuple[datetime, Path, pl.DataFrame]] = []
    pattern = "kernel.universe.selection_gates-*/data.parquet"
    for path in (data_root / "accepted").glob(pattern):
        frame = pl.read_parquet(path)
        if "session_date" not in frame.columns:
            continue
        dates = frame.get_column("session_date").unique().to_list()
        if dates != [trade_date]:
            continue
        matches.append(
            (
                datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
                path,
                frame,
            )
        )
    if not matches:
        raise FileNotFoundError(f"no locked selection exists for {trade_date}")
    _, path, frame = max(matches, key=lambda item: item[0])
    return path, frame


def _profile_payload(value: LongGreenExpansion) -> dict[str, Any]:
    payload: dict[str, Any] = asdict(value)
    for key, item in payload.items():
        if isinstance(item, datetime):
            payload[key] = item.isoformat()
        elif isinstance(item, float):
            payload[key] = round(item, 6)
    return payload


def _ranking_key(row: dict[str, object]) -> tuple[bool, float, int]:
    score = row.get("score")
    rank = row.get("selection_rank")
    return (
        not bool(row.get("qualified")),
        -(float(score) if isinstance(score, (int, float)) else -1.0),
        int(rank) if isinstance(rank, int) else 1_000_000,
    )


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_date)
    parser.add_argument("--asof", type=_utc_timestamp)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    args = parser.parse_args()

    now = datetime.now(UTC)
    as_of: datetime = args.asof or now
    if as_of > now + timedelta(seconds=1):
        raise ValueError("asof cannot be in the future")
    trade_date: date = args.trade_date or as_of.astimezone(EASTERN).date()
    schedule = build_xnys_schedule(trade_date, trade_date)
    if schedule.height != 1:
        raise RuntimeError("target XNYS session is unavailable")
    session = schedule.row(0, named=True)
    market_open = session["market_open_utc"]
    market_close = session["market_close_utc"]
    if not isinstance(market_open, datetime) or not isinstance(market_close, datetime):
        raise RuntimeError("calendar timestamps are invalid")
    query_end = min(as_of, market_close)
    if query_end <= market_open:
        raise RuntimeError("regular session has not started")

    selection_path, selection = _latest_selection(args.data_root, trade_date)
    required = {"symbol", "pass_gate", "selection_rank", "rvol"}
    if missing := required - set(selection.columns):
        raise ValueError(f"selection missing columns: {sorted(missing)}")
    candidates = selection.filter(pl.col("pass_gate")).sort("selection_rank")
    policy = stock_data_policy_from_env()
    rows: list[dict[str, object]] = []
    for candidate in candidates.select(sorted(required)).iter_rows(named=True):
        symbol = str(candidate["symbol"])
        try:
            frame, coverage = fetch_sparse_bars_for_monitoring(
                (symbol,),
                market_open,
                query_end,
                feed=policy.feed,
            )
            one_minute = resample_completed_bars(
                frame,
                schedule,
                interval_minutes=1,
                as_of_utc=query_end,
            )
            profile = build_long_green_expansion(
                one_minute,
                trade_date=trade_date,
                premarket_rvol=(
                    float(candidate["rvol"])
                    if isinstance(candidate["rvol"], (int, float))
                    else None
                ),
            )
            if profile is None:
                raise DownloadError("profile unavailable")
            coverage_symbols = coverage.get("symbols")
            coverage_row = (
                coverage_symbols[0]
                if isinstance(coverage_symbols, list)
                and coverage_symbols
                and isinstance(coverage_symbols[0], dict)
                else {}
            )
            rows.append(
                {
                    "symbol": symbol,
                    "selection_rank": candidate["selection_rank"],
                    "premarket_rvol": candidate["rvol"],
                    "qualified": profile.qualified,
                    "score": round(profile.score, 6),
                    "profile": _profile_payload(profile),
                    "coverage_status": coverage.get("status"),
                    "missing_minute_count": coverage_row.get(
                        "missing_minute_count"
                    ),
                    "availability": "observed",
                }
            )
        except (DownloadError, ValueError, RuntimeError):
            rows.append(
                {
                    "symbol": symbol,
                    "selection_rank": candidate["selection_rank"],
                    "premarket_rvol": candidate["rvol"],
                    "qualified": False,
                    "score": None,
                    "profile": None,
                    "coverage_status": "unavailable",
                    "missing_minute_count": None,
                    "availability": "fail_closed",
                }
            )

    ranked = sorted(rows, key=_ranking_key)
    qualified = [row for row in ranked if row["qualified"] is True]
    output = {
        "schema_version": "long_green_watchlist.v1",
        "trade_date": trade_date.isoformat(),
        "observed_at_utc": query_end.astimezone(UTC).isoformat(),
        "selection_snapshot": selection_path.parent.name,
        "candidate_count": len(rows),
        "qualified_count": len(qualified),
        "qualified_symbols": [row["symbol"] for row in qualified],
        "watchlist": ranked,
        "safety": {
            "advisory_only": True,
            "automatic_order_authorized": False,
            "incomplete_buckets_excluded": True,
            "locked_selection_pool_only": True,
        },
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
