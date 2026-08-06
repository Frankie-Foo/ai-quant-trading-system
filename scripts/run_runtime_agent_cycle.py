"""Refresh runtime news agents, deterministic supervisor, and safety envelopes."""

from __future__ import annotations

import argparse
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from pydantic import SecretStr

from data_plane.providers.alpaca_direct import DirectAlpacaMarketDataClient
from execution.alpaca_paper import DirectAlpacaPaperBroker
from operations.autonomous_paper_config import load_autonomous_paper_config
from operations.livermore_push import LivermorePushClient
from operations.runtime_agent_cycle import run_runtime_agent_cycle
from operations.runtime_agent_safety import RuntimeAgentRole
from research.providers.deepseek import DEEPSEEK_MODEL, DeepSeekClient
from schedule.runtime import JsonEventLogger, ProcessLock
from scripts.run_autonomous_paper_session import direct_paper_credentials

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
        default=ROOT / "runs" / "runtime-agent-cycle.lock",
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
    load_dotenv(ROOT / ".env")
    args = _parser().parse_args()
    if not 10 <= args.interval_seconds <= 30:
        raise ValueError("interval-seconds must be in [10, 30]")
    if args.max_seconds is not None and args.max_seconds <= 0:
        raise ValueError("max-seconds must be positive")
    config = load_autonomous_paper_config(args.config)
    key_id, secret_key = direct_paper_credentials(os.environ)
    market = DirectAlpacaMarketDataClient(
        key_id=key_id,
        secret_key=secret_key,
    )
    broker = DirectAlpacaPaperBroker(
        key_id=key_id,
        secret_key=secret_key,
        writes_enabled=False,
    )
    deepseek = DeepSeekClient.from_env()
    push = LivermorePushClient(
        app_id=os.getenv("VPS_LIVERMORE_APP_ID", "").strip(),
        app_secret=SecretStr(
            os.getenv("VPS_LIVERMORE_APP_SECRET", "")
        ),
        channel_id=os.getenv("VPS_LIVERMORE_CHANNEL_ID", "").strip(),
    )
    completions = {
        RuntimeAgentRole.CATALYST: (
            lambda prompt: deepseek.complete_json(prompt, max_tokens=1200)
        ),
        RuntimeAgentRole.RED_TEAM: (
            lambda prompt: deepseek.complete_json(prompt, max_tokens=1200)
        ),
    }
    logger = JsonEventLogger(service="runtime_agent_cycle")
    deadline = (
        None
        if args.max_seconds is None
        else time.monotonic() + float(args.max_seconds)
    )
    try:
        with ProcessLock(args.lock_file):
            while True:
                started = time.monotonic()
                summary = run_runtime_agent_cycle(
                    bundles=config.plans,
                    agent_root=args.agent_root,
                    push_health_path=args.push_health,
                    observed_at_utc=datetime.now(UTC),
                    market=market,
                    broker=broker,
                    push=push,
                    model_id=DEEPSEEK_MODEL,
                    completions=completions,
                )
                logger.emit(
                    "runtime_agent_cycle_completed",
                    plans=summary.plans,
                    healthy_envelopes=summary.healthy_envelopes,
                    input_errors=summary.input_errors,
                    push_healthy=summary.push_healthy,
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
    finally:
        push.close()
        broker.close()
        market.close()


if __name__ == "__main__":
    raise SystemExit(main())
