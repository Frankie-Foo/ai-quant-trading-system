"""Continuously aggregate three agent artifacts into fail-closed safety envelopes."""

from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime
from pathlib import Path

from operations.autonomous_paper_config import load_autonomous_paper_config
from operations.runtime_safety_refresh import refresh_runtime_safety_envelopes
from schedule.runtime import JsonEventLogger, ProcessLock

ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--agent-root",
        type=Path,
        default=ROOT / "runs" / "runtime-agents",
    )
    parser.add_argument(
        "--push-health",
        type=Path,
        default=ROOT / "runs" / "runtime-agents" / "push-health.json",
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=ROOT / "runs" / "runtime-safety-refresh.lock",
    )
    parser.add_argument("--interval-seconds", type=float, default=15.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--max-seconds",
        type=float,
        help="Stop cleanly after this many seconds; omit for continuous operation.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not 5 <= args.interval_seconds <= 15:
        raise ValueError("interval-seconds must be in [5, 15]")
    if args.max_seconds is not None and args.max_seconds <= 0:
        raise ValueError("max-seconds must be positive")
    config = load_autonomous_paper_config(args.config)
    logger = JsonEventLogger(service="runtime_safety_refresh")
    deadline = (
        None
        if args.max_seconds is None
        else time.monotonic() + float(args.max_seconds)
    )
    with ProcessLock(args.lock_file):
        while True:
            started = time.monotonic()
            summaries = refresh_runtime_safety_envelopes(
                bundles=config.plans,
                agent_root=args.agent_root,
                push_health_path=args.push_health,
                observed_at_utc=datetime.now(UTC),
            )
            logger.emit(
                "runtime_safety_envelopes_refreshed",
                plans=len(summaries),
                healthy=sum(item.agents_healthy for item in summaries),
                push_healthy=sum(item.push_healthy for item in summaries),
                input_errors=sum(item.input_errors for item in summaries),
                orders_submitted=0,
            )
            if args.once:
                return 0
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return 0
            else:
                remaining = args.interval_seconds
            sleep_for = max(
                0.0,
                args.interval_seconds - (time.monotonic() - started),
            )
            time.sleep(min(remaining, sleep_for))


if __name__ == "__main__":
    raise SystemExit(main())
