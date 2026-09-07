from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from kernel.strategy_policy import build_strategy_policy, write_strategy_policy
from operations.loop_integration.contracts import LoopPolicyCandidate
from operations.loop_integration.policy_consumer import install_shadow_candidate

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _payload(*, production_eligible: bool = False) -> dict[str, object]:
    return {
        "schema_version": "strategy_policy_candidate.v4",
        "mode": "PAPER_ONLY",
        "strategy_revision_id": "strategy-revision-1",
        "fingerprint": "f" * 64,
        "advisory_rule": {"decision": "watch", "explanation": "read only"},
        "allowed_parameter_overrides": {"universe.min_rvol": 3.5},
        "forbidden_execution_fields": {
            "trading_policy": {"stop_loss": 0.5},
            "broker_account": "must-never-be-consumed",
        },
        "production_eligible": production_eligible,
        "allow_order_execution": False,
    }


def _candidate(payload: dict[str, object]) -> LoopPolicyCandidate:
    return LoopPolicyCandidate.model_validate(
        {
            "id": "candidate-1",
            "artifact_type": "strategy_policy_candidate",
            "market_scope": "US-equity",
            "status": "candidate",
            "effective_at": NOW.isoformat(),
            "available_at": NOW.isoformat(),
            "source_run_id": "run-1",
            "payload": payload,
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        }
    )


def test_shared_v4_schema_is_identical_when_loop_checkout_is_present() -> None:
    local = json.loads((ROOT / "config/schemas/strategy_policy_candidate.v4.json").read_text())
    loop_path = ROOT.parent / "vertu-loop-platform/config/schemas/strategy_policy_candidate.v4.json"
    if loop_path.exists():
        assert local == json.loads(loop_path.read_text())
    assert local["additionalProperties"] is False
    assert local["properties"]["allowed_parameter_overrides"]["additionalProperties"] is False


def test_v4_candidate_installs_only_allowlisted_shadow_parameter(tmp_path: Path) -> None:
    active_path = tmp_path / "active.json"
    challenger_path = tmp_path / "challenger.json"
    active = build_strategy_policy(
        version="selection-v1",
        status="active",
        min_rvol=3.0,
        created_at_utc=NOW,
        approved_by="owner",
        approved_at_utc=NOW,
    )
    write_strategy_policy(active_path, active)

    challenger = install_shadow_candidate(
        _candidate(_payload()),
        active_path=active_path,
        challenger_path=challenger_path,
        installed_at_utc=NOW,
    )

    assert challenger.status == "shadow"
    assert challenger.min_rvol == 3.5
    assert challenger.source_snapshot_ids == (
        "candidate-1",
        "strategy-revision-1",
        "f" * 64,
    )
    assert json.loads(challenger_path.read_text())["parameter_overrides"] == {
        "universe.min_rvol": 3.5
    }
    assert json.loads(active_path.read_text())["status"] == "active"
    assert "broker_account" not in challenger_path.read_text()


def test_v4_candidate_rejects_attempted_production_authority() -> None:
    with pytest.raises(ValidationError, match="production eligibility"):
        _candidate(_payload(production_eligible=True))
