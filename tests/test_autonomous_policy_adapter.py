from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from execution.alpaca_paper import PaperPosition
from execution.autonomous_paper_session import AutonomousPaperPlan
from kernel.adaptive_trade_plan import RealtimePlanFacts
from kernel.intraday_policy import (
    DecisionMetric,
    EntryRoute,
    IntradayPolicy,
    PolicyAction,
)
from operations.autonomous_policy_adapter import (
    AutonomousPolicyEvidence,
    AutonomousPolicySnapshotFactory,
    RuntimeSafetyEnvelope,
    load_runtime_safety_envelope,
    write_runtime_safety_envelope,
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
        max_spread_ratio=Decimal("0.0025"),
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
        a_plus_plus_approved=False,
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


def _envelope(*, expires_at: datetime | None = None) -> RuntimeSafetyEnvelope:
    return RuntimeSafetyEnvelope(
        trade_date=TRADE_DATE,
        symbol="XYZ",
        generated_at_utc=OBSERVED - timedelta(seconds=5),
        expires_at_utc=expires_at or OBSERVED + timedelta(seconds=25),
        negative_news_clear=True,
        material_negative=False,
        agents_healthy=True,
        push_healthy=True,
        source_snapshot_ids=("news-20260729", "agents-20260729"),
        provenance="runtime.safety-envelope.v1",
    )


def test_fresh_safety_envelope_builds_auditable_entry_snapshot() -> None:
    snapshot = AutonomousPolicySnapshotFactory().build(
        plan=_plan(),
        evidence=_evidence(),
        facts=_facts(),
        envelope=_envelope(),
        position=None,
        account_equity=Decimal("100000"),
    )

    assert snapshot.policy.catalyst.value == 88.0
    assert snapshot.policy.order_flow.value == 81.0
    assert snapshot.policy.order_flow.provenance.endswith(
        "nbbo_top_of_book.v1"
    )
    assert snapshot.policy.execution.value is not None
    assert snapshot.policy.execution.value > 90.0
    assert snapshot.policy.technical_structure_valid is True
    assert snapshot.policy.negative_news_clear is True
    assert snapshot.policy.agents_healthy is True
    assert snapshot.policy.push_healthy is True
    assert snapshot.policy.has_position is False
    assert snapshot.quote_provenance == "alpaca.sip.nbbo"
    assert IntradayPolicy().evaluate(snapshot.policy).action is PolicyAction.ENTER_PROBE


def test_stale_safety_envelope_fails_closed_and_exits_an_existing_position() -> None:
    position = PaperPosition(
        symbol="XYZ",
        qty="21",
        side="long",
        market_value="2142",
        avg_entry_price="100",
        current_price="102",
    )
    snapshot = AutonomousPolicySnapshotFactory().build(
        plan=_plan(),
        evidence=_evidence(),
        facts=_facts(),
        envelope=_envelope(expires_at=OBSERVED - timedelta(microseconds=1)),
        position=position,
        account_equity=Decimal("100000"),
    )

    assert snapshot.policy.has_position is True
    assert snapshot.policy.negative_news_clear is None
    assert snapshot.policy.agents_healthy is False
    assert snapshot.policy.push_healthy is False
    decision = IntradayPolicy().evaluate(snapshot.policy)
    assert decision.action is PolicyAction.EXIT
    assert decision.reasons == ("required_agent_unhealthy",)


def test_position_stage_uses_plan_reference_not_a_moving_live_ask() -> None:
    position = PaperPosition(
        symbol="XYZ",
        qty="21",
        side="long",
        market_value="2310",
        avg_entry_price="102.01",
        current_price="110",
    )
    facts = _facts()
    moved_facts = RealtimePlanFacts(
        **{
            **facts.__dict__,
            "bid": 109.98,
            "ask": 110.02,
            "last_price": 110.0,
        }
    )

    snapshot = AutonomousPolicySnapshotFactory().build(
        plan=_plan(),
        evidence=_evidence(),
        facts=moved_facts,
        envelope=_envelope(),
        position=position,
        account_equity=Decimal("100000"),
    )

    assert snapshot.policy.position_fraction == 0.25


def test_missing_safety_envelope_blocks_a_flat_account_entry() -> None:
    snapshot = AutonomousPolicySnapshotFactory().build(
        plan=_plan(),
        evidence=_evidence(),
        facts=_facts(),
        envelope=None,
        position=None,
        account_equity=Decimal("100000"),
    )

    decision = IntradayPolicy().evaluate(snapshot.policy)
    assert decision.action is PolicyAction.OBSERVE
    assert "required_agent_unhealthy" in decision.blockers
    assert "negative_news_not_cleared" in decision.blockers


def test_runtime_safety_envelope_round_trips_through_strict_atomic_json(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime-safety.json"

    write_runtime_safety_envelope(path, _envelope())
    loaded = load_runtime_safety_envelope(path)

    assert loaded == _envelope()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["unexpected"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected fields"):
        load_runtime_safety_envelope(path)


def test_runtime_safety_envelope_rejects_future_generation_time() -> None:
    with pytest.raises(ValueError, match="generated_at"):
        AutonomousPolicySnapshotFactory().build(
            plan=_plan(),
            evidence=_evidence(),
            facts=_facts(),
            envelope=RuntimeSafetyEnvelope(
                trade_date=TRADE_DATE,
                symbol="XYZ",
                generated_at_utc=OBSERVED + timedelta(seconds=1),
                expires_at_utc=OBSERVED + timedelta(seconds=30),
                negative_news_clear=True,
                material_negative=False,
                agents_healthy=True,
                push_healthy=True,
                source_snapshot_ids=("future",),
                provenance="runtime.safety-envelope.v1",
            ),
            position=None,
            account_equity=Decimal("100000"),
        )
