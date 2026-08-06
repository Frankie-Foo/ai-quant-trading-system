from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from operations.runtime_agent_safety import (
    PushHealthEvidence,
    RuntimeAgentAssessment,
    RuntimeAgentRole,
    RuntimeAgentVerdict,
    assemble_runtime_safety_envelope,
    load_push_health_evidence,
    load_runtime_agent_assessment,
    write_push_health_evidence,
    write_runtime_agent_assessment,
)

TRADE_DATE = date(2026, 7, 29)
NOW = datetime(2026, 7, 29, 14, 0, tzinfo=UTC)


def _assessment(
    role: RuntimeAgentRole,
    *,
    verdict: RuntimeAgentVerdict = RuntimeAgentVerdict.CLEAR,
    healthy: bool = True,
    material_negative: bool = False,
    negative_news_clear: bool | None = None,
    expires_at: datetime | None = None,
) -> RuntimeAgentAssessment:
    if negative_news_clear is None and role is not RuntimeAgentRole.SUPERVISOR:
        negative_news_clear = verdict is RuntimeAgentVerdict.CLEAR
    return RuntimeAgentAssessment(
        trade_date=TRADE_DATE,
        symbol="XYZ",
        role=role,
        generated_at_utc=NOW - timedelta(seconds=5),
        expires_at_utc=expires_at or NOW + timedelta(seconds=25),
        verdict=verdict,
        healthy=healthy,
        negative_news_clear=negative_news_clear,
        material_negative=material_negative,
        model_id=f"model-{role.value}",
        prompt_sha256="a" * 64,
        source_snapshot_ids=(f"snapshot-{role.value}",),
        provenance=f"runtime.agent.{role.value}.v1",
    )


def _push(*, healthy: bool = True) -> PushHealthEvidence:
    return PushHealthEvidence(
        generated_at_utc=NOW - timedelta(seconds=2),
        expires_at_utc=NOW + timedelta(seconds=28),
        healthy=healthy,
        source_snapshot_id="push-drill-20260729",
        provenance="operations.livermore.health.v1",
    )


def test_three_fresh_clear_agents_and_push_create_tradeable_envelope() -> None:
    envelope = assemble_runtime_safety_envelope(
        trade_date=TRADE_DATE,
        symbol="XYZ",
        observed_at_utc=NOW,
        assessments=tuple(
            _assessment(role)
            for role in (
                RuntimeAgentRole.CATALYST,
                RuntimeAgentRole.RED_TEAM,
                RuntimeAgentRole.SUPERVISOR,
            )
        ),
        push_health=_push(),
    )

    assert envelope.agents_healthy is True
    assert envelope.negative_news_clear is True
    assert envelope.material_negative is False
    assert envelope.push_healthy is True
    assert len(envelope.source_snapshot_ids) == 5


def test_missing_required_agent_is_explicitly_fail_closed() -> None:
    envelope = assemble_runtime_safety_envelope(
        trade_date=TRADE_DATE,
        symbol="XYZ",
        observed_at_utc=NOW,
        assessments=(
            _assessment(RuntimeAgentRole.CATALYST),
            _assessment(RuntimeAgentRole.RED_TEAM),
        ),
        push_health=_push(),
    )

    assert envelope.agents_healthy is False
    assert envelope.negative_news_clear is None
    assert envelope.material_negative is False
    assert "missing=supervisor" in envelope.provenance


def test_stale_agent_is_not_relabelled_as_healthy() -> None:
    envelope = assemble_runtime_safety_envelope(
        trade_date=TRADE_DATE,
        symbol="XYZ",
        observed_at_utc=NOW,
        assessments=(
            _assessment(RuntimeAgentRole.CATALYST),
            _assessment(
                RuntimeAgentRole.RED_TEAM,
                expires_at=NOW - timedelta(microseconds=1),
            ),
            _assessment(RuntimeAgentRole.SUPERVISOR),
        ),
        push_health=_push(),
    )

    assert envelope.agents_healthy is False
    assert envelope.negative_news_clear is None
    assert "stale=red_team" in envelope.provenance


def test_verified_material_negative_survives_aggregation() -> None:
    envelope = assemble_runtime_safety_envelope(
        trade_date=TRADE_DATE,
        symbol="XYZ",
        observed_at_utc=NOW,
        assessments=(
            _assessment(RuntimeAgentRole.CATALYST),
            _assessment(
                RuntimeAgentRole.RED_TEAM,
                verdict=RuntimeAgentVerdict.BLOCK,
                negative_news_clear=False,
                material_negative=True,
            ),
            _assessment(RuntimeAgentRole.SUPERVISOR),
        ),
        push_health=_push(),
    )

    assert envelope.agents_healthy is True
    assert envelope.negative_news_clear is False
    assert envelope.material_negative is True


def test_unhealthy_agent_cannot_publish_a_clear_verdict() -> None:
    with pytest.raises(ValueError, match="unhealthy"):
        _assessment(
            RuntimeAgentRole.CATALYST,
            verdict=RuntimeAgentVerdict.CLEAR,
            healthy=False,
            negative_news_clear=True,
        )


def test_agent_and_push_evidence_round_trip_through_strict_atomic_json(
    tmp_path: Path,
) -> None:
    assessment_path = tmp_path / "agents" / "catalyst.json"
    push_path = tmp_path / "push.json"

    write_runtime_agent_assessment(
        assessment_path,
        _assessment(RuntimeAgentRole.CATALYST),
    )
    write_push_health_evidence(push_path, _push())

    assert load_runtime_agent_assessment(assessment_path) == _assessment(
        RuntimeAgentRole.CATALYST
    )
    assert load_push_health_evidence(push_path) == _push()
    payload = json.loads(assessment_path.read_text(encoding="utf-8"))
    payload["unexpected"] = True
    assessment_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected fields"):
        load_runtime_agent_assessment(assessment_path)
