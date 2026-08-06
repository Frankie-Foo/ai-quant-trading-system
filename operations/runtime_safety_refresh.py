"""Refresh fail-closed runtime safety envelopes from three agent artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from operations.autonomous_paper_config import AutonomousPaperPlanBundle
from operations.autonomous_policy_adapter import write_runtime_safety_envelope
from operations.runtime_agent_safety import (
    RuntimeAgentAssessment,
    RuntimeAgentRole,
    assemble_runtime_safety_envelope,
    load_push_health_evidence,
    load_runtime_agent_assessment,
)


@dataclass(frozen=True)
class RuntimeSafetyRefreshSummary:
    symbol: str
    agents_loaded: int
    input_errors: int
    agents_healthy: bool
    push_healthy: bool


def refresh_runtime_safety_envelopes(
    *,
    bundles: tuple[AutonomousPaperPlanBundle, ...],
    agent_root: Path,
    push_health_path: Path,
    observed_at_utc: datetime,
) -> tuple[RuntimeSafetyRefreshSummary, ...]:
    """Build every safety envelope even when inputs are missing or malformed."""

    _require_utc(observed_at_utc)
    try:
        push_health = load_push_health_evidence(push_health_path)
        push_error = 0
    except ValueError:
        push_health = None
        push_error = 1

    summaries: list[RuntimeSafetyRefreshSummary] = []
    for bundle in bundles:
        assessments: list[RuntimeAgentAssessment] = []
        errors = push_error
        evidence_root = (
            agent_root
            / bundle.plan.trade_date.isoformat()
            / bundle.plan.symbol
        )
        for role in RuntimeAgentRole:
            try:
                assessment = load_runtime_agent_assessment(
                    evidence_root / f"{role.value}.json"
                )
            except ValueError:
                errors += 1
                continue
            assessments.append(assessment)
        envelope = assemble_runtime_safety_envelope(
            trade_date=bundle.plan.trade_date,
            symbol=bundle.plan.symbol,
            observed_at_utc=observed_at_utc,
            assessments=tuple(assessments),
            push_health=push_health,
        )
        write_runtime_safety_envelope(
            bundle.safety_envelope_path,
            envelope,
        )
        summaries.append(
            RuntimeSafetyRefreshSummary(
                symbol=bundle.plan.symbol,
                agents_loaded=len(assessments),
                input_errors=errors,
                agents_healthy=envelope.agents_healthy,
                push_healthy=envelope.push_healthy,
            )
        )
    return tuple(summaries)


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("observed_at_utc must be timezone-aware UTC")
