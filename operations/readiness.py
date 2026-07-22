"""Auditable product-maturity gates; eligibility never arms live trading."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Attestation(FrozenModel):
    passed: bool = False
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def passed_requires_evidence(self) -> Attestation:
        if self.passed and not self.evidence_refs:
            raise ValueError("a passed attestation requires an evidence reference")
        return self


class MaturityEvidence(FrozenModel):
    asof_utc: datetime
    point_in_time_history_sessions: int = Field(default=0, ge=0)
    net_labeled_trade_count: int = Field(default=0, ge=0)
    purged_oos_fold_count: int = Field(default=0, ge=0)
    quote_cost_coverage: float = Field(default=0, ge=0, le=1)
    paper_trading_sessions: int = Field(default=0, ge=0)
    reconciliation_match_rate: float = Field(default=0, ge=0, le=1)
    duplicate_order_count: int = Field(default=0, ge=0)
    full_market_realtime_data: Attestation = Attestation()
    historical_data_license: Attestation = Attestation()
    paper_broker_access: Attestation = Attestation()
    kill_switch_drill: Attestation = Attestation()
    alert_delivery_drill: Attestation = Attestation()
    backup_restore_drill: Attestation = Attestation()
    broker_recovery_drill: Attestation = Attestation()
    secrets_rotated: Attestation = Attestation()
    owner_risk_signoff: Attestation = Attestation()
    compliance_signoff: Attestation = Attestation()
    live_broker_permission: Attestation = Attestation()

    @field_validator("asof_utc")
    @classmethod
    def utc_only(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("asof_utc must be timezone-aware UTC")
        return value


class ProductStage(StrEnum):
    RESEARCH_ONLY = "research_only"
    PAPER_ELIGIBLE = "paper_eligible"
    LIVE_ELIGIBLE = "live_eligible"


class GateGroup(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class MaturityGate(FrozenModel):
    name: str = Field(min_length=1)
    group: GateGroup
    passed: bool
    observed: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    owner: Literal["program", "frank", "joint"]
    evidence_refs: tuple[str, ...] = ()


class ReadinessReport(FrozenModel):
    stage: ProductStage
    paper_eligible: bool
    live_eligible: bool
    approved_for_live: Literal[False] = False
    gates: tuple[MaturityGate, ...]


def _attestation_gate(
    name: str,
    group: GateGroup,
    value: Attestation,
    *,
    owner: Literal["program", "frank", "joint"],
    expected: str,
) -> MaturityGate:
    return MaturityGate(
        name=name,
        group=group,
        passed=value.passed,
        observed="verified" if value.passed else "not verified",
        expected=expected,
        owner=owner,
        evidence_refs=value.evidence_refs,
    )


def assess_product_readiness(evidence: MaturityEvidence) -> ReadinessReport:
    paper_gates = (
        MaturityGate(
            name="point_in_time_history_sessions",
            group=GateGroup.PAPER,
            passed=evidence.point_in_time_history_sessions >= 252,
            observed=str(evidence.point_in_time_history_sessions),
            expected=">=252 XNYS sessions",
            owner="program",
        ),
        MaturityGate(
            name="net_labeled_trade_count",
            group=GateGroup.PAPER,
            passed=evidence.net_labeled_trade_count >= 100,
            observed=str(evidence.net_labeled_trade_count),
            expected=">=100 uncensored cost-complete labels",
            owner="program",
        ),
        MaturityGate(
            name="purged_oos_fold_count",
            group=GateGroup.PAPER,
            passed=evidence.purged_oos_fold_count >= 5,
            observed=str(evidence.purged_oos_fold_count),
            expected=">=5 chronological purged OOS folds",
            owner="program",
        ),
        MaturityGate(
            name="quote_cost_coverage",
            group=GateGroup.PAPER,
            passed=evidence.quote_cost_coverage >= 0.99,
            observed=f"{evidence.quote_cost_coverage:.4f}",
            expected=">=0.99",
            owner="program",
        ),
        _attestation_gate(
            "full_market_realtime_data",
            GateGroup.PAPER,
            evidence.full_market_realtime_data,
            owner="frank",
            expected="licensed full-market realtime feed verified",
        ),
        _attestation_gate(
            "historical_data_license",
            GateGroup.PAPER,
            evidence.historical_data_license,
            owner="frank",
            expected="historical bars/quotes usage rights verified",
        ),
        _attestation_gate(
            "paper_broker_access",
            GateGroup.PAPER,
            evidence.paper_broker_access,
            owner="frank",
            expected="paper-only broker account verified",
        ),
        _attestation_gate(
            "kill_switch_drill",
            GateGroup.PAPER,
            evidence.kill_switch_drill,
            owner="joint",
            expected="broker-write kill switch drill passed",
        ),
        _attestation_gate(
            "alert_delivery_drill",
            GateGroup.PAPER,
            evidence.alert_delivery_drill,
            owner="joint",
            expected="critical alert delivery and acknowledgement tested",
        ),
        _attestation_gate(
            "backup_restore_drill",
            GateGroup.PAPER,
            evidence.backup_restore_drill,
            owner="joint",
            expected="immutable data and ledger restore drill passed",
        ),
    )
    live_gates = (
        MaturityGate(
            name="paper_trading_sessions",
            group=GateGroup.LIVE,
            passed=evidence.paper_trading_sessions >= 60,
            observed=str(evidence.paper_trading_sessions),
            expected=">=60 completed paper sessions",
            owner="program",
        ),
        MaturityGate(
            name="reconciliation_match_rate",
            group=GateGroup.LIVE,
            passed=evidence.reconciliation_match_rate == 1.0,
            observed=f"{evidence.reconciliation_match_rate:.6f}",
            expected="1.0 for submitted orders and fills",
            owner="program",
        ),
        MaturityGate(
            name="duplicate_order_count",
            group=GateGroup.LIVE,
            passed=evidence.duplicate_order_count == 0,
            observed=str(evidence.duplicate_order_count),
            expected="0",
            owner="program",
        ),
        _attestation_gate(
            "broker_recovery_drill",
            GateGroup.LIVE,
            evidence.broker_recovery_drill,
            owner="joint",
            expected="disconnect/restart/reconciliation drill passed",
        ),
        _attestation_gate(
            "secrets_rotated",
            GateGroup.LIVE,
            evidence.secrets_rotated,
            owner="frank",
            expected="all previously exposed credentials rotated",
        ),
        _attestation_gate(
            "owner_risk_signoff",
            GateGroup.LIVE,
            evidence.owner_risk_signoff,
            owner="frank",
            expected="capital and risk limits explicitly signed off",
        ),
        _attestation_gate(
            "compliance_signoff",
            GateGroup.LIVE,
            evidence.compliance_signoff,
            owner="frank",
            expected="jurisdiction, tax, licensing, and compliance reviewed",
        ),
        _attestation_gate(
            "live_broker_permission",
            GateGroup.LIVE,
            evidence.live_broker_permission,
            owner="frank",
            expected="live broker API permission explicitly verified",
        ),
    )
    paper_eligible = all(gate.passed for gate in paper_gates)
    live_eligible = paper_eligible and all(gate.passed for gate in live_gates)
    stage = (
        ProductStage.LIVE_ELIGIBLE
        if live_eligible
        else ProductStage.PAPER_ELIGIBLE
        if paper_eligible
        else ProductStage.RESEARCH_ONLY
    )
    return ReadinessReport(
        stage=stage,
        paper_eligible=paper_eligible,
        live_eligible=live_eligible,
        approved_for_live=False,
        gates=(*paper_gates, *live_gates),
    )
