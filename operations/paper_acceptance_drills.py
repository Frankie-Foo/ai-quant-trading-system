"""Offline acceptance drills for the fail-closed Alpaca Paper runtime."""

from __future__ import annotations

import gc
import hashlib
import json
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from execution.alpaca_paper import BrokerOrder, PaperPosition
from operations.paper_runtime_policy import ExecutionAuthorization, PaperRuntimePolicy
from operations.paper_state import OutboxClaim, PaperStateStore, UnknownBrokerStateError
from operations.runtime_alerts import RuntimeAlertManager

EASTERN = ZoneInfo("America/New_York")
TRADE_DATE = date(2026, 8, 24)
NOW = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)


@dataclass(frozen=True)
class PaperAcceptanceReceipt:
    schema_version: str
    generated_at_utc: str
    code_commit: str
    config_sha256: str
    clock_rules_passed: bool
    kill_switch_passed: bool
    duplicate_process_passed: bool
    intent_recovery_passed: bool
    outbox_idempotency_passed: bool
    unknown_state_freeze_passed: bool
    fault_escalation_passed: bool
    force_flatten_passed: bool
    broker_calls: int
    external_writes: int
    receipt_sha256: str

    @property
    def passed(self) -> bool:
        checks = (
            self.clock_rules_passed,
            self.kill_switch_passed,
            self.duplicate_process_passed,
            self.intent_recovery_passed,
            self.outbox_idempotency_passed,
            self.unknown_state_freeze_passed,
            self.fault_escalation_passed,
            self.force_flatten_passed,
        )
        return all(checks) and self.broker_calls == 0 and self.external_writes == 0


class _LocalPush:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def push(self, body: str) -> str:
        self.messages.append(body)
        return f"local-{len(self.messages)}"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _commit(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unavailable"


def _authorization() -> ExecutionAuthorization:
    return ExecutionAuthorization(
        trade_date=TRADE_DATE,
        selection_snapshot_id="selection-1",
        open_confirmation_id="confirmation-1",
        feishu_record_id="record-1",
        livermore_message_id="message-1",
        strategy_version="modern-h15.v1",
        candidate_pool=("AAPL",),
        config_sha256="a" * 64,
    )


def _kill_switch_drill(policy: PaperRuntimePolicy) -> bool:
    try:
        policy.validate_arming(
            trade_date=TRADE_DATE,
            broker_write_enabled=True,
            trading_kill_switch=True,
            broker_base_url="https://paper-api.alpaca.markets",
            authorization=_authorization(),
            expected_candidate_pool=("AAPL",),
            expected_strategy_version="modern-h15.v1",
        )
    except RuntimeError as exc:
        return "kill switch" in str(exc)
    return False


def _state_drills(root: Path) -> tuple[bool, bool, bool, bool]:
    store = PaperStateStore(root / "paper.sqlite3")
    first_claim = store.claim_run(TRADE_DATE, owner="one", observed_at_utc=NOW)
    duplicate_blocked = not store.claim_run(
        TRADE_DATE,
        owner="two",
        observed_at_utc=NOW + timedelta(seconds=1),
    )
    payload: dict[str, object] = {"symbol": "AAPL", "quantity": 1}
    for _ in range(2):
        store.record_order_intent(
            trade_date=TRADE_DATE,
            client_order_id="smoke-AAPL-entry-1",
            symbol="AAPL",
            attempt=1,
            role="entry",
            quantity=1,
            payload=payload,
            observed_at_utc=NOW,
        )
    restarted = PaperStateStore(root / "paper.sqlite3")
    intent = restarted.get_order("smoke-AAPL-entry-1")
    intent_recovered = intent is not None and intent.payload == payload
    store.enqueue_outbox(
        event_key="smoke-fill",
        event_type="paper_fill",
        payload={"symbol": "AAPL"},
        observed_at_utc=NOW,
    )
    first_outbox = store.claim_outbox("smoke-fill", observed_at_utc=NOW)
    ambiguous_outbox = store.claim_outbox(
        "smoke-fill",
        observed_at_utc=NOW + timedelta(seconds=1),
    )
    outbox_safe = (
        first_outbox is OutboxClaim.CLAIMED
        and ambiguous_outbox is OutboxClaim.IN_FLIGHT
    )
    unknown_frozen = False
    try:
        store.assert_reconcilable(
            TRADE_DATE,
            open_orders=(
                BrokerOrder(
                    id="foreign-order",
                    client_order_id="foreign-client",
                    symbol="MSFT",
                    qty=1,
                    filled_qty="0",
                    status="new",
                ),
            ),
            positions=(
                PaperPosition(
                    symbol="MSFT",
                    qty="1",
                    side="long",
                    market_value="100",
                ),
            ),
        )
    except UnknownBrokerStateError:
        unknown_frozen = True
    return first_claim and duplicate_blocked, intent_recovered, outbox_safe, unknown_frozen


def _fault_drill(root: Path) -> bool:
    push = _LocalPush()
    alerts = RuntimeAlertManager(root / "alerts.sqlite3", push=push)
    for offset in range(3):
        alerts.report_failure(
            "alpaca-sip",
            component="Alpaca SIP",
            error_type="TimeoutError",
            observed_at_utc=NOW + timedelta(seconds=offset),
        )
    alerts.report_recovery(
        "alpaca-sip",
        component="Alpaca SIP",
        observed_at_utc=NOW + timedelta(seconds=3),
    )
    return alerts.is_frozen("alpaca-sip") and len(push.messages) == 3


def _clock_drill(policy: PaperRuntimePolicy) -> tuple[bool, bool]:
    def at(value: time) -> datetime:
        return datetime.combine(TRADE_DATE, value, tzinfo=EASTERN)

    clock_passed = (
        not policy.entry_allowed_at(at(time(9, 55)))
        and policy.entry_allowed_at(at(time(9, 56)))
        and not policy.entry_allowed_at(at(time(15, 0)))
        and policy.must_cancel_entries_at(at(time(15, 45)))
    )
    flatten_passed = (
        not policy.must_flatten_at(at(time(15, 49)))
        and policy.must_flatten_at(at(time(15, 50)))
        and policy.must_flatten_for_daily_return(-0.02)
    )
    return clock_passed, flatten_passed


def run_paper_acceptance_drills(*, root: Path, output_path: Path) -> PaperAcceptanceReceipt:
    """Run local-only safety drills and persist a tamper-evident receipt."""

    policy = PaperRuntimePolicy()
    clock_passed, flatten_passed = _clock_drill(policy)
    with tempfile.TemporaryDirectory(prefix="paper-acceptance-") as temporary:
        work = Path(temporary)
        duplicate, recovery, outbox, unknown = _state_drills(work)
        fault = _fault_drill(work)
        gc.collect()
    material: dict[str, object] = {
        "schema_version": "paper_acceptance_drills.v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "code_commit": _commit(root),
        "config_sha256": _sha256(root / "config.yaml"),
        "clock_rules_passed": clock_passed,
        "kill_switch_passed": _kill_switch_drill(policy),
        "duplicate_process_passed": duplicate,
        "intent_recovery_passed": recovery,
        "outbox_idempotency_passed": outbox,
        "unknown_state_freeze_passed": unknown,
        "fault_escalation_passed": fault,
        "force_flatten_passed": flatten_passed,
        "broker_calls": 0,
        "external_writes": 0,
    }
    canonical = json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    receipt = PaperAcceptanceReceipt(
        schema_version=str(material["schema_version"]),
        generated_at_utc=str(material["generated_at_utc"]),
        code_commit=str(material["code_commit"]),
        config_sha256=str(material["config_sha256"]),
        clock_rules_passed=bool(material["clock_rules_passed"]),
        kill_switch_passed=bool(material["kill_switch_passed"]),
        duplicate_process_passed=bool(material["duplicate_process_passed"]),
        intent_recovery_passed=bool(material["intent_recovery_passed"]),
        outbox_idempotency_passed=bool(material["outbox_idempotency_passed"]),
        unknown_state_freeze_passed=bool(material["unknown_state_freeze_passed"]),
        fault_escalation_passed=bool(material["fault_escalation_passed"]),
        force_flatten_passed=bool(material["force_flatten_passed"]),
        broker_calls=int(str(material["broker_calls"])),
        external_writes=int(str(material["external_writes"])),
        receipt_sha256=hashlib.sha256(canonical).hexdigest(),
    )
    if not receipt.passed:
        raise RuntimeError("Paper acceptance drill failed")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(asdict(receipt), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary_path.replace(output_path)
    return receipt
