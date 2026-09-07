"""Native daily discovery and actual read-only Paper reconciliation; no manual metadata."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from kernel.strategy_policy import load_strategy_policy
from operations.loop_integration.daily_provider import produce_daily
from operations.loop_integration.execution_summary import load_execution_index, write_pinned_json
from operations.loop_integration.review_builder import load_accepted_snapshot


def run(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--trade-date", type=date.fromisoformat, required=True)
    parser.add_argument("--active-policy", type=Path)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    parser.add_argument("--selection-cutoff-utc", type=datetime.fromisoformat)
    parser.add_argument("--history-only", action="store_true")
    args = parser.parse_args(argv)
    # Local immutable indexes are independent of whether today's plan exists.
    entries = {}
    paths = sorted(
        (args.run_root / "loop").glob("*/execution-index-*.json"),
        key=lambda path: path.stat().st_mtime_ns,
    )
    for path in paths:
        if not path.resolve().is_relative_to(args.run_root.resolve()):
            raise ValueError("history path escapes explicit run root")
        for entry in load_execution_index(path, path.stem.removeprefix("execution-index-")):
            if entry.trade_date <= args.trade_date:
                entries[(entry.trade_date, entry.strategy_sha256)] = entry.model_dump(mode="json")
    history_path, history_hash = write_pinned_json(
        args.run_root / "loop" / "history",
        "execution-index",
        {"executions": list(entries.values())},
    )
    if args.history_only:
        return {
            "status": "prepared",
            "execution_index_path": str(history_path),
            "execution_index_sha256": history_hash,
        }
    if args.active_policy is None:
        parser.error("--active-policy is required for daily production")
    cutoff = args.selection_cutoff_utc
    if cutoff is None:
        if args.data_root is None:
            parser.error("--data-root or --selection-cutoff-utc is required")
        candidates = []
        for path in (args.data_root / "accepted").glob(
            "research.intraday_selection_postmortem-*/data.parquet"
        ):
            snapshot, frame = load_accepted_snapshot(path)
            if frame.get_column("session_date").unique().to_list() == [args.trade_date]:
                stamps = frame.get_column("selection_cutoff_utc").unique().to_list()
                if len(stamps) != 1 or not isinstance(stamps[0], datetime):
                    raise ValueError("accepted review selection cutoff is unavailable/ambiguous")
                candidates.append((snapshot.asof_utc, stamps[0]))
        if not candidates:
            raise ValueError("accepted selection cutoff evidence unavailable")
        cutoff = max(candidates)[1]
    active = load_strategy_policy(args.active_policy, required_status="active")
    return produce_daily(
        run_root=args.run_root,
        trade_date=args.trade_date,
        active_policy_hash=active.policy_hash,
        selection_cutoff_utc=cutoff,
        as_of=args.as_of or datetime.now(UTC),
        output_dir=args.run_root / "loop" / str(args.trade_date),
        prior_execution_index_path=history_path,
        prior_execution_index_sha256=history_hash,
    )


def main() -> None:
    try:
        print(json.dumps(run()))
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "blocked", "error_type": type(exc).__name__, "orders_submitted": 0}
            )
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
