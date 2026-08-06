"""Keep the autonomous local SIP store current through direct Alpaca REST."""

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
        default=ROOT / "runs" / "autonomous-sip-refresher.lock",
    )
    parser.add_argument(
        "--refresh-lock-file",
        type=Path,
        default=ROOT / "runs" / "autonomous-sip-warmup.lock",
    )
    parser.add_argument("--history-days", type=int, default=10)
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    parser.add_argument(
        "--max-seconds",
        type=float,
        help="Stop cleanly after this many seconds; omit for continuous operation.",
    )
    return parser


def refresh_command(
    args: argparse.Namespace,
    *,
    incremental: bool,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "scripts.warm_autonomous_sip_store",
        "--config",
        str(args.config),
        "--sip-db",
        str(args.sip_db),
        "--lock-file",
        str(args.refresh_lock_file),
        "--history-days",
        str(args.history_days),
    ]
    if incremental:
        command.append("--incremental")
    return command


def main() -> int:
    args = _parser().parse_args()
    if args.history_days < 7:
        raise ValueError("history-days must be at least 7")
    if args.interval_seconds < 15:
        raise ValueError("interval-seconds must be at least 15")
    if args.max_seconds is not None and args.max_seconds <= 0:
        raise ValueError("max-seconds must be positive")
    logger = JsonEventLogger(service="autonomous_sip_refresher")
    deadline = (
        None
        if args.max_seconds is None
        else time.monotonic() + float(args.max_seconds)
    )
    incremental = False
    with ProcessLock(args.lock_file):
        while True:
            started = time.monotonic()
            completed = subprocess.run(
                refresh_command(args, incremental=incremental),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=max(120.0, args.interval_seconds * 4),
            )
            logger.emit(
                "autonomous_sip_refresh_completed",
                ok=completed.returncode == 0,
                returncode=completed.returncode,
                mode="incremental" if incremental else "full",
                orders_submitted=0,
            )
            if not incremental and completed.returncode != 0:
                return 1
            incremental = True
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
