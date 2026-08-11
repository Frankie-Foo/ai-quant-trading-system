"""Run the fail-closed autonomous intraday strategy against Alpaca Paper only."""

from __future__ import annotations

import argparse
import os
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr

from execution.alpaca_paper import (
    CloudPaperBroker,
    DirectAlpacaPaperBroker,
)
from execution.autonomous_paper_session import (
    AutonomousPaperBroker,
    PaperSessionLedger,
    PaperSessionOrchestrator,
)
from execution.ibkr_paper_broker import IBKRPaperBroker
from execution.ibkr_tws_adapter import OfficialIbapiPaperAdapter
from execution.settings import ExecutionSettings
from execution.sip_store import SipEventStore
from operations.adaptive_plan_adapters import (
    PlanEvidence,
    SipStoreMarketFactsAdapter,
)
from operations.autonomous_notifications import (
    AutonomousNotificationLedger,
    AutonomousPaperNotifier,
    deliver_notification_or_fail_closed,
)
from operations.autonomous_paper_config import (
    AutonomousPaperRuntimeConfig,
    load_autonomous_paper_config,
)
from operations.autonomous_paper_runtime import AutonomousPaperRuntime
from operations.autonomous_policy_adapter import load_runtime_safety_envelope
from operations.feishu_base import FeishuBaseEventClient
from operations.livermore_push import LivermorePushClient
from operations.local_env import load_project_env
from schedule.runtime import JsonEventLogger, ProcessLock

ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--broker-mode",
        choices=("direct", "cloud", "ibkr"),
        default="direct",
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        default=ROOT / "runs" / "autonomous-paper.sqlite3",
    )
    parser.add_argument(
        "--sip-db",
        type=Path,
        default=ROOT / "runs" / "sip-stream.sqlite3",
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=ROOT / "runs" / "autonomous-paper.lock",
    )
    parser.add_argument(
        "--notification-db",
        type=Path,
        default=ROOT / "runs" / "autonomous-notifications.sqlite3",
    )
    parser.add_argument(
        "--push-health",
        type=Path,
        default=ROOT / "runs" / "runtime-agents" / "push-health.json",
    )
    parser.add_argument(
        "--arm-paper",
        action="store_true",
        help="Second explicit switch required before any Alpaca Paper write.",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--max-seconds",
        type=float,
        help="Stop cleanly after this many seconds; omit for continuous operation.",
    )
    return parser


def resolve_paper_authorization(
    *,
    arm_paper: bool,
    broker_write_enabled: bool,
    trading_kill_switch: bool,
) -> bool:
    if not arm_paper:
        return False
    if not broker_write_enabled:
        raise RuntimeError("--arm-paper also requires BROKER_WRITE_ENABLED=true")
    if trading_kill_switch:
        raise RuntimeError("--arm-paper is blocked while the trading kill switch is active")
    return True


def direct_paper_credentials(
    environment: Mapping[str, str],
) -> tuple[SecretStr, SecretStr]:
    key_id = _first_present(
        environment,
        "ALPACA_PAPER_KEY_ID",
        "APCA_API_KEY_ID",
        "ALPACA_API_KEY_ID",
        "ALPACA_API_KEY",
    )
    secret_key = _first_present(
        environment,
        "ALPACA_PAPER_SECRET_KEY",
        "APCA_API_SECRET_KEY",
        "ALPACA_API_SECRET_KEY",
        "ALPACA_SECRET_KEY",
    )
    if key_id is None or secret_key is None:
        raise RuntimeError("Alpaca Paper credentials are incomplete")
    return SecretStr(key_id), SecretStr(secret_key)


def _first_present(
    environment: Mapping[str, str],
    *names: str,
) -> str | None:
    for name in names:
        value = environment.get(name, "").strip()
        if value:
            return value
    return None


def _boolean_environment(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise RuntimeError(f"{name} must be true or false")


def _broker(
    *,
    mode: str,
    paper_authorized: bool,
    state_db: Path,
    environment: Mapping[str, str],
) -> AutonomousPaperBroker:
    if mode == "direct":
        key_id, secret_key = direct_paper_credentials(environment)
        return DirectAlpacaPaperBroker(
            key_id=key_id,
            secret_key=secret_key,
            writes_enabled=paper_authorized,
        )
    if mode == "ibkr":
        host, client_id, paper_account = ibkr_paper_profile(environment)
        broker = IBKRPaperBroker(
            path=state_db.with_name("ibkr-paper-orders.sqlite3"),
            transport=OfficialIbapiPaperAdapter(
                api_read_only=False,
                expected_account_id=paper_account,
            ),
            paper_account=paper_account,
            writes_enabled=paper_authorized,
        )
        broker.connect(host=host, client_id=client_id)
        return broker
    settings = ExecutionSettings()  # type: ignore[call-arg]
    return CloudPaperBroker(
        base_url=settings.cloud_platform_base_url,
        token=settings.cloud_paper_api_token,
        writes_enabled=paper_authorized,
    )


def ibkr_paper_profile(
    environment: Mapping[str, str],
) -> tuple[str, int, str]:
    """Read only the non-secret 4002 connection profile for IBKR Paper."""

    host = str(environment.get("IBKR_PAPER_HOST", "")).strip()
    client_id_text = str(environment.get("IBKR_PAPER_CLIENT_ID", "")).strip()
    account = str(environment.get("IBKR_PAPER_ACCOUNT", "")).strip().upper()
    if not host or len(host) > 253 or re.fullmatch(r"[A-Za-z0-9.-]+", host) is None:
        raise RuntimeError("IBKR Paper host is invalid")
    if re.fullmatch(r"\d+", client_id_text) is None:
        raise RuntimeError("IBKR Paper client id is invalid")
    client_id = int(client_id_text)
    if client_id > 2_147_483_647:
        raise RuntimeError("IBKR Paper client id is invalid")
    if re.fullmatch(r"DU[A-Z0-9-]{4,30}", account) is None:
        raise RuntimeError("IBKR Paper account is invalid")
    return host, client_id, account


def _livermore_push(
    environment: Mapping[str, str],
) -> LivermorePushClient:
    return LivermorePushClient(
        app_id=environment.get("VPS_LIVERMORE_APP_ID", "").strip(),
        app_secret=SecretStr(environment.get("VPS_LIVERMORE_APP_SECRET", "")),
        channel_id=environment.get("VPS_LIVERMORE_CHANNEL_ID", "").strip(),
    )


def _feishu_base(
    environment: Mapping[str, str],
) -> FeishuBaseEventClient | None:
    return FeishuBaseEventClient.from_environment(environment)


def _market_adapter(
    config: AutonomousPaperRuntimeConfig,
    *,
    sip_db: Path,
) -> SipStoreMarketFactsAdapter:
    evidence: dict[str, PlanEvidence] = {}
    for bundle in config.plans:
        catalyst = bundle.evidence.catalyst.value
        evidence[bundle.plan.plan_id] = PlanEvidence(
            benchmark_symbol=bundle.benchmark_symbol,
            sector_symbol=bundle.sector_symbol,
            catalyst_score=None if catalyst is None else catalyst / 100.0,
            provenance=(
                f"{bundle.market_context_provenance}|{bundle.evidence.catalyst.provenance}"
            ),
        )
    return SipStoreMarketFactsAdapter(
        store=SipEventStore(sip_db),
        evidence=evidence,
    )


def main() -> int:
    load_project_env(ROOT)
    args = _parser().parse_args()
    if args.max_seconds is not None and args.max_seconds <= 0:
        raise ValueError("max-seconds must be positive")
    config = load_autonomous_paper_config(args.config)
    paper_authorized = resolve_paper_authorization(
        arm_paper=bool(args.arm_paper),
        broker_write_enabled=_boolean_environment(
            "BROKER_WRITE_ENABLED",
            default=False,
        ),
        trading_kill_switch=_boolean_environment(
            "TRADING_KILL_SWITCH",
            default=True,
        ),
    )
    broker = _broker(
        mode=str(args.broker_mode),
        paper_authorized=paper_authorized,
        state_db=args.state_db,
        environment=os.environ,
    )
    push = _livermore_push(os.environ)
    base = _feishu_base(os.environ)
    notifier = AutonomousPaperNotifier(
        push=push,
        ledger=AutonomousNotificationLedger(args.notification_db),
        base=base,
        broker_identity=str(getattr(broker, "broker_identity", "unknown")),
    )
    orchestrator = PaperSessionOrchestrator(
        broker=broker,
        ledger=PaperSessionLedger(args.state_db),
        paper_authorized=paper_authorized,
        owned_symbols=frozenset(bundle.plan.symbol for bundle in config.plans),
    )
    runtime = AutonomousPaperRuntime(
        plans=config.plans,
        market=_market_adapter(config, sip_db=args.sip_db),
        broker=broker,
        orchestrator=orchestrator,
        envelope_loader=load_runtime_safety_envelope,
    )
    plans_by_id = {bundle.plan.plan_id: bundle.plan for bundle in config.plans}
    logger = JsonEventLogger(service="autonomous_paper_session")
    deadline = None if args.max_seconds is None else time.monotonic() + float(args.max_seconds)
    try:
        with ProcessLock(args.lock_file):
            while True:
                started = time.monotonic()
                observed_at = datetime.now(UTC)
                try:
                    outcomes = runtime.tick_once(
                        observed_at_utc=observed_at,
                    )
                    for outcome in outcomes:
                        logger.emit(
                            "autonomous_paper_plan_evaluated",
                            plan_id=outcome.plan_id,
                            symbol=outcome.symbol,
                            action=outcome.result.action.value,
                            reasons=list(outcome.result.reasons),
                            degraded_reasons=list(outcome.degraded_reasons),
                            daily_return_pct=str(outcome.result.daily_return * 100),
                            day_locked=outcome.result.day_locked,
                            submitted_order_ids=list(outcome.result.submitted_order_ids),
                            paper_writes_authorized=paper_authorized,
                            live_trading_authorized=False,
                        )
                        delivery = deliver_notification_or_fail_closed(
                            plan=plans_by_id[outcome.plan_id],
                            result=outcome.result,
                            observed_at_utc=observed_at,
                            notifier=notifier,
                            push_health_path=args.push_health,
                            fail_closed=runtime,
                        )
                        if delivery.notified:
                            logger.emit(
                                "autonomous_paper_notification_delivered",
                                plan_id=outcome.plan_id,
                                symbol=outcome.symbol,
                                action=outcome.result.action.value,
                                sender_type="bot",
                            )
                        if delivery.failure_reason is not None:
                            fallback = delivery.fail_closed_result
                            logger.emit(
                                "autonomous_paper_notification_failed_closed",
                                level="error",
                                plan_id=outcome.plan_id,
                                symbol=outcome.symbol,
                                failure_reason=delivery.failure_reason,
                                fallback_action=(
                                    None if fallback is None else fallback.action.value
                                ),
                                live_trading_authorized=False,
                            )
                except Exception as exc:
                    logger.emit(
                        "autonomous_paper_runtime_degraded",
                        level="error",
                        error_type=type(exc).__name__,
                        paper_writes_authorized=paper_authorized,
                        live_trading_authorized=False,
                    )
                    for bundle in config.plans:
                        try:
                            fallback = runtime.fail_closed_plan(
                                plan_id=bundle.plan.plan_id,
                                observed_at_utc=observed_at,
                                reason="autonomous_runtime_failed",
                            )
                        except Exception as fail_exc:
                            logger.emit(
                                "autonomous_paper_runtime_fail_closed_failed",
                                level="critical",
                                plan_id=bundle.plan.plan_id,
                                error_type=type(fail_exc).__name__,
                                paper_writes_authorized=paper_authorized,
                                live_trading_authorized=False,
                            )
                        else:
                            logger.emit(
                                "autonomous_paper_runtime_failed_closed",
                                level="critical",
                                plan_id=bundle.plan.plan_id,
                                fallback_action=fallback.action.value,
                                paper_writes_authorized=paper_authorized,
                                live_trading_authorized=False,
                            )
                    if args.once:
                        return 1
                if args.once:
                    return 0
                elapsed = time.monotonic() - started
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return 0
                    time.sleep(
                        min(
                            remaining,
                            max(0.0, config.poll_seconds - elapsed),
                        )
                    )
                else:
                    time.sleep(max(0.0, config.poll_seconds - elapsed))
    finally:
        push.close()
        close = getattr(broker, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    raise SystemExit(main())
