from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from execution.alpaca_paper import PaperAccount, PaperPosition
from execution.autonomous_paper_session import (
    AutonomousPaperPlan,
    PaperSessionResult,
    PaperSessionSnapshot,
    SessionAction,
)
from kernel.adaptive_trade_plan import RealtimePlanFacts
from kernel.intraday_policy import DecisionMetric, EntryRoute
from operations.autonomous_paper_config import AutonomousPaperPlanBundle
from operations.autonomous_paper_runtime import AutonomousPaperRuntime
from operations.autonomous_policy_adapter import (
    AutonomousPolicyEvidence,
    RuntimeSafetyEnvelope,
)

TRADE_DATE = date(2026, 7, 29)
OBSERVED = datetime(2026, 7, 29, 14, 0, tzinfo=UTC)


def _metric(value: float | None, *, name: str) -> DecisionMetric:
    return DecisionMetric(
        value=value,
        asof_utc=OBSERVED - timedelta(minutes=30),
        provenance=f"accepted.selection:{name}",
    )


def _plan() -> AutonomousPaperPlan:
    return AutonomousPaperPlan(
        plan_id="auto-20260729-XYZ",
        symbol="XYZ",
        trade_date=TRADE_DATE,
        reference_price=Decimal("102.01"),
        hard_stop=Decimal("98.00"),
        max_notional_fraction=Decimal("0.20"),
        full_risk_fraction=Decimal("0.0035"),
        source_snapshot_ids=("selection-20260729",),
        provenance="accepted.selection:auto-plan",
    )


def _evidence() -> AutonomousPolicyEvidence:
    return AutonomousPolicyEvidence(
        route=EntryRoute.CATALYST,
        catalyst=_metric(88.0, name="catalyst"),
        factor=_metric(72.0, name="factor"),
        right_tail=_metric(76.0, name="right-tail"),
        first_target_reward_r=2.5,
        weighted_expected_reward_r=3.2,
        reward_risk_provenance="owner-approved-plan:r-multiple.v1",
    )


def _facts() -> RealtimePlanFacts:
    return RealtimePlanFacts(
        observed_at_utc=OBSERVED,
        quote_ts_utc=OBSERVED - timedelta(seconds=1),
        bid=101.99,
        ask=102.01,
        last_price=102.0,
        session_vwap=101.50,
        completed_one_minute_bar_utc=OBSERVED - timedelta(minutes=1),
        one_minute_trigger=True,
        five_minute_confirmed=True,
        fifteen_minute_confirmed=True,
        green_volume_ratio=0.72,
        relative_strength=0.03,
        benchmark_above_vwap=True,
        sector_above_vwap=True,
        market_risk_off=False,
        order_flow_imbalance=0.42,
        catalyst_score=0.88,
        data_complete=True,
        order_flow_confirmation_score=81.0,
        order_flow_provenance="alpaca.sip|tick_rule.v1|nbbo_top_of_book.v1",
        quote_provenance="alpaca.sip.nbbo",
    )


def _envelope() -> RuntimeSafetyEnvelope:
    return RuntimeSafetyEnvelope(
        trade_date=TRADE_DATE,
        symbol="XYZ",
        generated_at_utc=OBSERVED - timedelta(seconds=5),
        expires_at_utc=OBSERVED + timedelta(seconds=25),
        negative_news_clear=True,
        material_negative=False,
        agents_healthy=True,
        push_healthy=True,
        source_snapshot_ids=("news-20260729",),
        provenance="runtime.safety-envelope.v1",
    )


class FakeRuntimeMarket:
    def __init__(self, facts: RealtimePlanFacts | Exception):
        self.facts = facts

    def read(
        self,
        plan: AutonomousPaperPlan,
        *,
        observed_at_utc: datetime,
    ) -> RealtimePlanFacts:
        del plan, observed_at_utc
        if isinstance(self.facts, Exception):
            raise self.facts
        return self.facts


class FakeRuntimeBroker:
    def __init__(self, position: PaperPosition | None = None):
        self.position = position

    def get_account(self) -> PaperAccount:
        return PaperAccount(
            status="ACTIVE",
            account_blocked=False,
            trading_blocked=False,
            equity="100000",
            last_equity="100000",
            buying_power="400000",
        )

    def list_positions(self) -> tuple[PaperPosition, ...]:
        return () if self.position is None else (self.position,)


class RecordingOrchestrator:
    def __init__(self) -> None:
        self.snapshots: list[PaperSessionSnapshot] = []
        self.failures: list[dict[str, object]] = []

    def tick(
        self,
        plan: AutonomousPaperPlan,
        snapshot: PaperSessionSnapshot,
    ) -> PaperSessionResult:
        del plan
        self.snapshots.append(snapshot)
        return _result(SessionAction.OBSERVE, "tick")

    def fail_closed(
        self,
        plan: AutonomousPaperPlan,
        *,
        observed_at_utc: datetime,
        reason: str,
        exit_bid: Decimal | None = None,
        quote_asof_utc: datetime | None = None,
        quote_provenance: str | None = None,
    ) -> PaperSessionResult:
        del plan
        self.failures.append(
            {
                "observed_at_utc": observed_at_utc,
                "reason": reason,
                "exit_bid": exit_bid,
                "quote_asof_utc": quote_asof_utc,
                "quote_provenance": quote_provenance,
            }
        )
        return _result(SessionAction.DATA_BLOCKED, reason)


class FailingSnapshotFactory:
    def build(self, **kwargs: object) -> object:
        del kwargs
        raise ValueError("snapshot is invalid")


def _result(action: SessionAction, reason: str) -> PaperSessionResult:
    return PaperSessionResult(
        action=action,
        decision=None,
        daily_return=Decimal("0"),
        day_locked=False,
        new_entries_allowed=False,
        cancelled_order_ids=(),
        flatten_order_ids=(),
        reasons=(reason,),
        provenance="test.runtime",
    )


def _bundle() -> AutonomousPaperPlanBundle:
    return AutonomousPaperPlanBundle(
        plan=_plan(),
        evidence=_evidence(),
        safety_envelope_path=Path("runtime-safety.json"),
        benchmark_symbol="SPY",
        sector_symbol="XLK",
        market_context_provenance="owner-plan:market-context.v1",
    )


def _position() -> PaperPosition:
    return PaperPosition(
        symbol="XYZ",
        qty="21",
        side="long",
        market_value="2142",
        avg_entry_price="100",
        current_price="102",
    )


def test_runtime_combines_market_safety_and_broker_authoritative_position() -> None:
    orchestrator = RecordingOrchestrator()
    runtime = AutonomousPaperRuntime(
        plans=(_bundle(),),
        market=FakeRuntimeMarket(_facts()),
        broker=FakeRuntimeBroker(_position()),
        orchestrator=orchestrator,
        envelope_loader=lambda path: _envelope(),
    )

    outcomes = runtime.tick_once(observed_at_utc=OBSERVED)

    assert len(outcomes) == 1
    assert outcomes[0].degraded_reasons == ()
    assert len(orchestrator.snapshots) == 1
    snapshot = orchestrator.snapshots[0]
    assert snapshot.policy.has_position is True
    assert snapshot.policy.agents_healthy is True


def test_missing_safety_file_is_fail_closed_without_crashing_loop() -> None:
    orchestrator = RecordingOrchestrator()

    def missing(path: Path) -> RuntimeSafetyEnvelope:
        raise FileNotFoundError(path)

    runtime = AutonomousPaperRuntime(
        plans=(_bundle(),),
        market=FakeRuntimeMarket(_facts()),
        broker=FakeRuntimeBroker(_position()),
        orchestrator=orchestrator,
        envelope_loader=missing,
    )

    outcome = runtime.tick_once(observed_at_utc=OBSERVED)[0]

    assert outcome.degraded_reasons == ("safety_envelope_unavailable",)
    snapshot = orchestrator.snapshots[0]
    assert snapshot.policy.agents_healthy is False
    assert snapshot.policy.negative_news_clear is None


def test_market_failure_uses_last_fresh_quote_for_fail_closed_exit() -> None:
    orchestrator = RecordingOrchestrator()
    market = FakeRuntimeMarket(_facts())
    runtime = AutonomousPaperRuntime(
        plans=(_bundle(),),
        market=market,
        broker=FakeRuntimeBroker(_position()),
        orchestrator=orchestrator,
        envelope_loader=lambda path: _envelope(),
    )
    runtime.tick_once(observed_at_utc=OBSERVED)
    market.facts = RuntimeError("provider secret must never enter logs")
    second_at = OBSERVED + timedelta(seconds=10)

    outcome = runtime.tick_once(observed_at_utc=second_at)[0]

    assert outcome.degraded_reasons == ("market_facts_unavailable",)
    assert orchestrator.failures == [
        {
            "observed_at_utc": second_at,
            "reason": "market_facts_unavailable",
            "exit_bid": Decimal("101.99"),
            "quote_asof_utc": OBSERVED - timedelta(seconds=1),
            "quote_provenance": "alpaca.sip.nbbo",
        }
    ]
    assert "secret" not in " ".join(outcome.result.reasons)


def test_market_failure_does_not_reuse_a_stale_cached_quote() -> None:
    orchestrator = RecordingOrchestrator()
    market = FakeRuntimeMarket(_facts())
    runtime = AutonomousPaperRuntime(
        plans=(_bundle(),),
        market=market,
        broker=FakeRuntimeBroker(_position()),
        orchestrator=orchestrator,
        envelope_loader=lambda path: _envelope(),
    )
    runtime.tick_once(observed_at_utc=OBSERVED)
    market.facts = OSError("offline")
    stale_at = OBSERVED + timedelta(seconds=31)

    runtime.tick_once(observed_at_utc=stale_at)

    assert orchestrator.failures[-1]["exit_bid"] is None
    assert orchestrator.failures[-1]["quote_asof_utc"] is None
    assert orchestrator.failures[-1]["quote_provenance"] is None


def test_snapshot_failure_is_fail_closed_without_escaping_tick() -> None:
    orchestrator = RecordingOrchestrator()
    runtime = AutonomousPaperRuntime(
        plans=(_bundle(),),
        market=FakeRuntimeMarket(_facts()),
        broker=FakeRuntimeBroker(_position()),
        orchestrator=orchestrator,
        envelope_loader=lambda path: _envelope(),
        snapshot_factory=FailingSnapshotFactory(),  # type: ignore[arg-type]
    )

    outcomes = runtime.tick_once(observed_at_utc=OBSERVED)

    assert outcomes[0].degraded_reasons == ("runtime_evaluation_failed",)
    assert orchestrator.failures[-1]["reason"] == "runtime_evaluation_failed"


def test_external_runtime_failure_reuses_current_quote_for_safe_exit() -> None:
    orchestrator = RecordingOrchestrator()
    runtime = AutonomousPaperRuntime(
        plans=(_bundle(),),
        market=FakeRuntimeMarket(_facts()),
        broker=FakeRuntimeBroker(_position()),
        orchestrator=orchestrator,
        envelope_loader=lambda path: _envelope(),
    )
    runtime.tick_once(observed_at_utc=OBSERVED)
    failure_at = OBSERVED + timedelta(seconds=10)

    result = runtime.fail_closed_plan(
        plan_id=_plan().plan_id,
        observed_at_utc=failure_at,
        reason="notification_push_failed",
    )

    assert result.reasons == ("notification_push_failed",)
    assert orchestrator.failures[-1] == {
        "observed_at_utc": failure_at,
        "reason": "notification_push_failed",
        "exit_bid": Decimal("101.99"),
        "quote_asof_utc": OBSERVED - timedelta(seconds=1),
        "quote_provenance": "alpaca.sip.nbbo",
    }


def test_runtime_rejects_a_tick_for_the_wrong_new_york_session() -> None:
    runtime = AutonomousPaperRuntime(
        plans=(_bundle(),),
        market=FakeRuntimeMarket(_facts()),
        broker=FakeRuntimeBroker(),
        orchestrator=RecordingOrchestrator(),
        envelope_loader=lambda path: _envelope(),
    )

    outcomes = runtime.tick_once(
        observed_at_utc=datetime(2026, 7, 30, 14, 0, tzinfo=UTC)
    )

    assert outcomes[0].degraded_reasons == ("trade_date_mismatch",)
    assert outcomes[0].result.action is SessionAction.DATA_BLOCKED
