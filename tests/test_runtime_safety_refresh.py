from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from execution.autonomous_paper_session import AutonomousPaperPlan
from kernel.intraday_policy import DecisionMetric, EntryRoute
from operations.autonomous_paper_config import AutonomousPaperPlanBundle
from operations.autonomous_policy_adapter import (
    AutonomousPolicyEvidence,
    load_runtime_safety_envelope,
)
from operations.runtime_agent_safety import (
    PushHealthEvidence,
    RuntimeAgentAssessment,
    RuntimeAgentRole,
    RuntimeAgentVerdict,
    write_push_health_evidence,
    write_runtime_agent_assessment,
)
from operations.runtime_safety_refresh import refresh_runtime_safety_envelopes

TRADE_DATE = date(2026, 7, 29)
NOW = datetime(2026, 7, 29, 14, 0, tzinfo=UTC)


def _bundle(tmp_path: Path) -> AutonomousPaperPlanBundle:
    metric = DecisionMetric(
        value=80.0,
        asof_utc=NOW - timedelta(minutes=10),
        provenance="accepted.metric.v1",
    )
    return AutonomousPaperPlanBundle(
        plan=AutonomousPaperPlan(
            plan_id="auto-20260729-XYZ",
            symbol="XYZ",
            trade_date=TRADE_DATE,
            reference_price=Decimal("100"),
            hard_stop=Decimal("98"),
            max_notional_fraction=Decimal("0.20"),
            full_risk_fraction=Decimal("0.0035"),
            source_snapshot_ids=("selection-1",),
            provenance="accepted.selection.v1",
            max_spread_ratio=Decimal("0.0025"),
        ),
        evidence=AutonomousPolicyEvidence(
            route=EntryRoute.CATALYST,
            catalyst=metric,
            factor=metric,
            right_tail=metric,
            first_target_reward_r=2.5,
            weighted_expected_reward_r=3.0,
            reward_risk_provenance="accepted.reward-risk.v1",
            a_plus_plus_approved=False,
        ),
        safety_envelope_path=tmp_path / "safety" / "XYZ.json",
        benchmark_symbol="SPY",
        sector_symbol="XLK",
        market_context_provenance="accepted.market-context.v1",
    )


def _assessment(role: RuntimeAgentRole) -> RuntimeAgentAssessment:
    return RuntimeAgentAssessment(
        trade_date=TRADE_DATE,
        symbol="XYZ",
        role=role,
        generated_at_utc=NOW - timedelta(seconds=5),
        expires_at_utc=NOW + timedelta(seconds=25),
        verdict=RuntimeAgentVerdict.CLEAR,
        healthy=True,
        negative_news_clear=(
            None if role is RuntimeAgentRole.SUPERVISOR else True
        ),
        material_negative=False,
        model_id=f"model-{role.value}",
        prompt_sha256="b" * 64,
        source_snapshot_ids=(f"snapshot-{role.value}",),
        provenance=f"runtime.agent.{role.value}.v1",
    )


def test_refresh_builds_tradeable_envelope_from_three_agents_and_push(
    tmp_path: Path,
) -> None:
    agent_root = tmp_path / "agents"
    day_root = agent_root / TRADE_DATE.isoformat() / "XYZ"
    for role in RuntimeAgentRole:
        write_runtime_agent_assessment(day_root / f"{role.value}.json", _assessment(role))
    push_path = agent_root / "push-health.json"
    write_push_health_evidence(
        push_path,
        PushHealthEvidence(
            generated_at_utc=NOW - timedelta(seconds=2),
            expires_at_utc=NOW + timedelta(seconds=28),
            healthy=True,
            source_snapshot_id="push-health-1",
            provenance="runtime.push-health.v1",
        ),
    )

    summaries = refresh_runtime_safety_envelopes(
        bundles=(_bundle(tmp_path),),
        agent_root=agent_root,
        push_health_path=push_path,
        observed_at_utc=NOW,
    )

    envelope = load_runtime_safety_envelope(
        tmp_path / "safety" / "XYZ.json"
    )
    assert summaries[0].agents_loaded == 3
    assert summaries[0].agents_healthy is True
    assert envelope.agents_healthy is True
    assert envelope.push_healthy is True


def test_refresh_writes_fail_closed_envelope_when_input_is_missing_or_invalid(
    tmp_path: Path,
) -> None:
    agent_root = tmp_path / "agents"
    day_root = agent_root / TRADE_DATE.isoformat() / "XYZ"
    day_root.mkdir(parents=True)
    (day_root / "catalyst.json").write_text("{broken", encoding="utf-8")

    summaries = refresh_runtime_safety_envelopes(
        bundles=(_bundle(tmp_path),),
        agent_root=agent_root,
        push_health_path=agent_root / "missing-push.json",
        observed_at_utc=NOW,
    )

    envelope = load_runtime_safety_envelope(
        tmp_path / "safety" / "XYZ.json"
    )
    assert summaries[0].agents_loaded == 0
    assert summaries[0].input_errors == 4
    assert envelope.agents_healthy is False
    assert envelope.negative_news_clear is None
    assert envelope.push_healthy is False
