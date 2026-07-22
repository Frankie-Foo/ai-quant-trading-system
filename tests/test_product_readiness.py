from __future__ import annotations

from datetime import UTC, datetime

from operations.readiness import (
    Attestation,
    MaturityEvidence,
    ProductStage,
    assess_product_readiness,
)

NOW = datetime(2026, 7, 21, 4, 0, tzinfo=UTC)


def _attested(name: str) -> Attestation:
    return Attestation(passed=True, evidence_refs=(f"evidence:{name}",))


def test_current_evidence_remains_research_only() -> None:
    evidence = MaturityEvidence(asof_utc=NOW)
    report = assess_product_readiness(evidence)

    assert report.stage is ProductStage.RESEARCH_ONLY
    assert report.paper_eligible is False
    assert report.live_eligible is False
    assert report.approved_for_live is False


def test_all_objective_and_attested_gates_reach_live_eligible_not_approved() -> None:
    yes = _attested("verified")
    evidence = MaturityEvidence(
        asof_utc=NOW,
        point_in_time_history_sessions=252,
        net_labeled_trade_count=100,
        purged_oos_fold_count=5,
        quote_cost_coverage=1.0,
        paper_trading_sessions=60,
        reconciliation_match_rate=1.0,
        duplicate_order_count=0,
        full_market_realtime_data=yes,
        historical_data_license=yes,
        paper_broker_access=yes,
        kill_switch_drill=yes,
        alert_delivery_drill=yes,
        backup_restore_drill=yes,
        broker_recovery_drill=yes,
        secrets_rotated=yes,
        owner_risk_signoff=yes,
        compliance_signoff=yes,
        live_broker_permission=yes,
    )
    report = assess_product_readiness(evidence)

    assert report.stage is ProductStage.LIVE_ELIGIBLE
    assert report.paper_eligible is True
    assert report.live_eligible is True
    assert report.approved_for_live is False
    assert all(gate.passed for gate in report.gates)
