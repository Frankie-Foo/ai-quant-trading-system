"""Backfill causal full-session SIP minute bars for historical candidates and proxies."""

from __future__ import annotations

import argparse
import json
import time
from datetime import date
from pathlib import Path

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DatasetSnapshot
from data_plane.providers.alpaca import fetch_bars, stock_data_policy_from_env
from data_plane.quality import BAR_SCHEMA_VERSION, audit_minute_bars
from data_plane.storage import persist_snapshot
from operations.local_env import load_project_env, project_data_root
from schedule.runtime import ProcessLock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "alpaca.sip.rth_1m"
DEFAULT_PROXIES = (
    "SPY",
    "QQQ",
    "IWM",
    "XLB",
    "XLC",
    "XLE",
    "XLF",
    "XLI",
    "XLK",
    "XLP",
    "XLRE",
    "XLU",
    "XLV",
    "XLY",
    "SMH",
)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _candidate_symbols(data_root: Path) -> dict[date, tuple[str, ...]]:
    result: dict[date, set[str]] = {}
    for path in (data_root / "accepted").glob("research.trading_episodes-*/data.parquet"):
        frame = pl.read_parquet(path, columns=["session_date", "symbol"])
        for session_date, symbol in frame.iter_rows():
            result.setdefault(session_date, set()).add(str(symbol).strip().upper())
    return {
        session_date: tuple(sorted(symbols))
        for session_date, symbols in sorted(result.items())
    }


def _cohort_symbols(
    data_root: Path, source: str = "research.h30_candidate_cohort"
) -> dict[date, tuple[str, ...]]:
    matches = [
        (_manifest(path).asof_utc, path)
        for path in (data_root / "accepted").glob(
            f"{source}-*/data.parquet"
        )
    ]
    if not matches:
        raise FileNotFoundError(f"candidate cohort is missing: {source}")
    _, path = max(matches, key=lambda item: item[0])
    frame = pl.read_parquet(path, columns=["session_date", "symbol"])
    return {
        row["session_date"]: tuple(sorted(row["symbol"]))
        for row in frame.group_by("session_date").agg(pl.col("symbol").unique()).iter_rows(
            named=True
        )
    }


def _manifest(path: Path) -> DatasetSnapshot:
    return DatasetSnapshot.model_validate_json(
        (path.parent / "manifest.json").read_text(encoding="utf-8")
    )


def _covered_symbols(data_root: Path) -> dict[date, set[str]]:
    result: dict[date, set[str]] = {}
    for path in (data_root / "accepted").glob(f"{SOURCE}-*/data.parquet"):
        frame = pl.read_parquet(path, columns=["symbol", "ts_utc"]).with_columns(
            pl.col("ts_utc")
            .dt.convert_time_zone("America/New_York")
            .dt.date()
            .alias("session_date")
        )
        for session_date, symbol in frame.select("session_date", "symbol").unique().iter_rows():
            result.setdefault(session_date, set()).add(str(symbol))
    return result


def plan_sessions(
    candidates: dict[date, tuple[str, ...]],
    *,
    start: date,
    end: date,
    proxies: tuple[str, ...] = DEFAULT_PROXIES,
) -> dict[date, tuple[str, ...]]:
    normalized_proxies = {symbol.strip().upper() for symbol in proxies if symbol.strip()}
    return {
        session_date: tuple(sorted(set(symbols) | normalized_proxies))
        for session_date, symbols in candidates.items()
        if start <= session_date <= end
    }


def main() -> int:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=_parse_date, required=True)
    parser.add_argument("--end", type=_parse_date, required=True)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    parser.add_argument("--pace-seconds", type=float, default=1.0)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument(
        "--candidate-source",
        choices=(
            "episodes",
            "h30-cohort",
            "counterfactual-cohort",
            "current-event-rvol",
        ),
        default="episodes",
    )
    parser.add_argument("--without-proxies", action="store_true")
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=ROOT / "runs" / "backfill-historical-rth.lock",
    )
    args = parser.parse_args()
    if args.end < args.start:
        raise ValueError("end date must not precede start date")
    if args.pace_seconds < 0:
        raise ValueError("pace seconds cannot be negative")

    if args.candidate_source == "h30-cohort":
        candidates = _cohort_symbols(args.data_root)
    elif args.candidate_source == "counterfactual-cohort":
        candidates = _cohort_symbols(
            args.data_root, "research.counterfactual_candidate_cohort"
        )
    elif args.candidate_source == "current-event-rvol":
        candidates = _cohort_symbols(
            args.data_root, "research.current_event_rvol_cohort"
        )
    else:
        candidates = _candidate_symbols(args.data_root)
    proxies = () if args.without_proxies else DEFAULT_PROXIES
    plan = plan_sessions(candidates, start=args.start, end=args.end, proxies=proxies)
    if not plan:
        raise FileNotFoundError("no historical trading episodes in requested range")
    covered = _covered_symbols(args.data_root)
    pending = {
        session_date: symbols
        for session_date, symbols in plan.items()
        if not set(symbols).issubset(covered.get(session_date, set()))
    }
    print(
        json.dumps(
            {
                "status": "plan",
                "sessions": len(plan),
                "pending_sessions": len(pending),
                "candidate_symbol_days": sum(len(candidates[item]) for item in plan),
                "requested_symbol_days": sum(len(symbols) for symbols in plan.values()),
                "source": SOURCE,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    if args.plan_only:
        return 0

    schedule = build_xnys_schedule(args.start, args.end).select(
        "trade_date", "market_open_utc", "market_close_utc"
    )
    sessions = {row["trade_date"]: row for row in schedule.iter_rows(named=True)}
    policy = stock_data_policy_from_env()
    failures: list[dict[str, object]] = []
    with ProcessLock(args.lock_file):
        for index, (session_date, symbols) in enumerate(sorted(pending.items()), start=1):
            row = sessions.get(session_date)
            if row is None:
                failures.append(
                    {
                        "trade_date": session_date.isoformat(),
                        "reason": "calendar_missing",
                    }
                )
                continue
            started = time.monotonic()
            frame = fetch_bars(
                symbols,
                row["market_open_utc"],
                row["market_close_utc"],
                feed=policy.feed,
            )
            provenance = f"{SOURCE}@{session_date.isoformat()}|feed={policy.feed}"
            checks = audit_minute_bars(
                frame,
                provenance=provenance,
                expected_symbols=symbols,
                research_approved=policy.feed == "sip" and policy.is_realtime,
            )
            snapshot, path = persist_snapshot(
                frame,
                root=args.data_root,
                source=SOURCE,
                schema_version=BAR_SCHEMA_VERSION,
                checks=checks,
            )
            if not snapshot.usable:
                failures.append(
                    {
                        "trade_date": session_date.isoformat(),
                        "reason": "quality_failed",
                        "path": str(path),
                    }
                )
            print(
                json.dumps(
                    {
                        "progress": f"{index}/{len(pending)}",
                        "trade_date": session_date.isoformat(),
                        "symbols": len(symbols),
                        "rows": frame.height,
                        "usable": snapshot.usable,
                        "dataset_id": snapshot.dataset_id,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            elapsed = time.monotonic() - started
            if index < len(pending) and elapsed < args.pace_seconds:
                time.sleep(args.pace_seconds - elapsed)
    print(json.dumps({"status": "complete", "failures": failures}, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
