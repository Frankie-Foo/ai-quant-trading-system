"""Periodically refresh causal SIP facts used by the adaptive monitor."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from schedule.runtime import JsonEventLogger, ProcessLock

ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--sip-db",
        type=Path,
        default=ROOT / "runs" / "sip-stream.sqlite3",
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=ROOT / "runs" / "adaptive-sip-refresher.lock",
    )
    parser.add_argument(
        "--refresh-lock-file",
        type=Path,
        default=ROOT / "runs" / "adaptive-sip-refresh.lock",
    )
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument(
        "--max-seconds",
        type=float,
        help="Stop cleanly after this many seconds; omit for continuous operation.",
    )
    return parser


def _refresh_command(args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        "-m",
        "scripts.warm_adaptive_sip_store",
        "--config",
        str(args.config),
        "--sip-db",
        str(args.sip_db),
        "--lock-file",
        str(args.refresh_lock_file),
    ]


def main() -> int:
    args = _parser().parse_args()
    if args.interval_seconds < 15:
        raise ValueError("interval-seconds must be at least 15")
    if args.max_seconds is not None and args.max_seconds <= 0:
        raise ValueError("max-seconds must be positive")

    logger = JsonEventLogger(service="adaptive_sip_refresher")
    deadline = (
        None
        if args.max_seconds is None
        else time.monotonic() + float(args.max_seconds)
    )
    with ProcessLock(args.lock_file):
        while True:
            started = time.monotonic()
            completed = subprocess.run(
                _refresh_command(args),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=max(60.0, args.interval_seconds * 2),
            )
            logger.emit(
                "adaptive_sip_refresh_completed",
                ok=completed.returncode == 0,
                returncode=completed.returncode,
                orders_submitted=0,
            )
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return 0
            else:
                remaining = args.interval_seconds
            sleep_for = max(0.0, args.interval_seconds - (time.monotonic() - started))
            time.sleep(min(remaining, sleep_for))


if __name__ == "__main__":
    raise SystemExit(main())
