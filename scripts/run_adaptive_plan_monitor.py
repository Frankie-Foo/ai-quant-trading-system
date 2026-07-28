"""Evaluate registered adaptive plans every 15 seconds without order authority."""

from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from execution.alpaca_paper import CloudPaperBroker
from execution.settings import ExecutionSettings
from execution.sip_store import SipEventStore
from operations.adaptive_plan_adapters import (
    CloudBrokerPositionAdapter,
    SipStoreMarketFactsAdapter,
)
from operations.adaptive_plan_config import load_adaptive_plan_config
from operations.adaptive_plan_coordinator import AdaptivePlanCoordinator
from operations.adaptive_plan_store import AdaptivePlanStore
from schedule.runtime import JsonEventLogger, ProcessLock

ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--state-db",
        type=Path,
        default=ROOT / "runs" / "adaptive-plans.sqlite3",
    )
    parser.add_argument(
        "--sip-db",
        type=Path,
        default=ROOT / "runs" / "sip-stream.sqlite3",
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=ROOT / "runs" / "adaptive-plan-monitor.lock",
    )
    parser.add_argument("--once", action="store_true")
    return parser


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = _parser().parse_args()
    config = load_adaptive_plan_config(args.config)
    store = AdaptivePlanStore(args.state_db)
    for plan in config.plans:
        store.register(plan)
    settings = ExecutionSettings()  # type: ignore[call-arg]
    broker = CloudPaperBroker(
        base_url=settings.cloud_platform_base_url,
        token=settings.cloud_paper_api_token,
        writes_enabled=False,
    )
    market = SipStoreMarketFactsAdapter(
        store=SipEventStore(args.sip_db),
        evidence=config.evidence,
    )
    coordinator = AdaptivePlanCoordinator(
        store=store,
        market=market,
        broker=CloudBrokerPositionAdapter(broker),
    )
    logger = JsonEventLogger(service="adaptive_plan_monitor")
    try:
        with ProcessLock(args.lock_file):
            while True:
                started = time.monotonic()
                observed_at = datetime.now(UTC)
                for plan in config.plans:
                    try:
                        result = coordinator.tick(
                            plan.plan_id,
                            observed_at_utc=observed_at,
                        )
                        logger.emit(
                            "adaptive_plan_evaluated",
                            plan_id=plan.plan_id,
                            symbol=plan.symbol,
                            action=result.decision.action.value,
                            state=result.decision.next_state.value,
                            material_revision=result.decision.material_revision,
                            sequence=result.sequence,
                            position_source=result.position_source,
                            orders_authorized=False,
                        )
                    except (
                        KeyError,
                        OSError,
                        RuntimeError,
                        ValueError,
                    ) as exc:
                        logger.emit(
                            "adaptive_plan_degraded",
                            plan_id=plan.plan_id,
                            symbol=plan.symbol,
                            error_type=type(exc).__name__,
                            orders_authorized=False,
                        )
                if args.once:
                    return 0
                elapsed = time.monotonic() - started
                time.sleep(max(0.0, config.poll_seconds - elapsed))
    finally:
        broker.close()


if __name__ == "__main__":
    raise SystemExit(main())
