from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml
from perp_risk.config import load_config
from perp_risk.models import (
    OutcomeRecord,
    Regime,
    RiskSnapshot,
    Scope,
    TargetAssessment,
)
from perp_risk.research import approve_candidate, propose_threshold_config, review_outcomes
from perp_risk.session import session_state
from perp_risk.store import RiskStore


def _snapshot(asof: datetime) -> RiskSnapshot:
    target = TargetAssessment(
        target_id="global-risk",
        scope=Scope.MARKET,
        regime=Regime.RISK_OFF,
        score=-50,
        confidence=0.8,
        coverage=0.8,
        liquidation_coverage=0,
        disagreement=0,
        available_sources=4,
        configured_sources=4,
        available_venues=2,
        candidate_multiplier=0.5,
        effective_multiplier=0.5,
        pending_windows=0,
        confirmation_windows=2,
        sources=(),
        asof_utc=asof,
    )
    return RiskSnapshot(
        skill_version="0.1.0",
        snapshot_id="snapshot",
        asof_utc=asof,
        data_cutoff_utc=asof,
        config_hash="hash",
        actionable=True,
        session_state="actionable",
        provider_status=(),
        targets=(target,),
        production_eligible=False,
        execution_eligible=False,
        orders_submitted=0,
    )


def test_xnys_session_handles_actionable_and_holiday() -> None:
    config = load_config().session

    actionable = session_state(
        datetime(2026, 7, 30, 14, tzinfo=UTC),
        config,
    )
    holiday = session_state(
        datetime(2026, 7, 4, 14, tzinfo=UTC),
        config,
    )

    assert actionable == (True, "actionable")
    assert holiday == (False, "market_closed")


def test_review_separates_benchmark_and_trade_outcomes(tmp_path: Path) -> None:
    asof = datetime(2026, 7, 30, 14, tzinfo=UTC)
    store = RiskStore(tmp_path / "review.sqlite3")
    snapshot = _snapshot(asof)
    store.persist_snapshot(snapshot, observations=(), states=())
    store.record_outcome(
        OutcomeRecord(
            snapshot_id=snapshot.snapshot_id,
            target_id="global-risk",
            kind="benchmark",
            observed_at_utc=asof,
            horizon_minutes=30,
            return_pct=-1,
        )
    )
    store.record_outcome(
        OutcomeRecord(
            snapshot_id=snapshot.snapshot_id,
            target_id="global-risk",
            kind="trade",
            observed_at_utc=asof,
            horizon_minutes=30,
            return_pct=0.5,
        )
    )

    try:
        report = review_outcomes(store)
    finally:
        store.close()

    assert report.benchmark_count == 1
    assert report.trade_count == 1
    assert report.directional_accuracy == 1
    assert report.average_overlay_contribution_pct == 0.5
    assert report.average_trade_return_pct == 0.5


def test_threshold_candidate_hash_can_be_human_approved(tmp_path: Path) -> None:
    asof = datetime(2026, 7, 30, 14, tzinfo=UTC)
    config = load_config()
    store = RiskStore(tmp_path / "review.sqlite3")
    snapshot = _snapshot(asof)
    store.persist_snapshot(snapshot, observations=(), states=())
    for index in range(100):
        store.record_outcome(
            OutcomeRecord(
                snapshot_id=snapshot.snapshot_id,
                target_id="global-risk",
                kind="benchmark",
                observed_at_utc=asof,
                horizon_minutes=30 + index,
                return_pct=-1,
            )
        )
    candidate_path = tmp_path / "candidate.yaml"
    destination = tmp_path / "approved.yaml"

    try:
        generated_path, report = propose_threshold_config(
            store=store,
            config=config,
            output=candidate_path,
        )
        candidate = type(config).model_validate(
            yaml.safe_load(generated_path.read_text(encoding="utf-8"))
        )
        approved = approve_candidate(
            store=store,
            candidate_path=generated_path,
            destination=destination,
            confirmation_hash=str(report["candidate_hash"]),
        )
    finally:
        store.close()

    assert report["candidate_hash"] == candidate.config_hash
    assert approved == destination.resolve()
    assert destination.read_text(encoding="utf-8") == candidate_path.read_text(encoding="utf-8")
