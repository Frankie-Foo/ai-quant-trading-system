from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from data_plane.contracts import DataQualityCheck, QualitySeverity
from data_plane.storage import persist_snapshot
from kernel.config import load_config
from kernel.strategy_policy import (
    build_strategy_policy,
    load_strategy_policy,
    write_strategy_policy,
)
from operations.loop_integration.client import (
    AuditOnlyBackfillRequired,
    LoopClient,
    LoopPreconditionError,
    LoopRunFailedError,
    build_loop_task,
)
from operations.loop_integration.contracts import (
    LoopBinding,
    LoopOutcomeEnvelope,
    LoopPolicyCandidate,
)
from operations.loop_integration.control_plane import (
    LoopControlPlaneManifest,
    config_sha256,
)
from operations.loop_integration.outbox import LoopOutbox
from operations.loop_integration.policy_consumer import install_shadow_candidate
from operations.loop_integration.review_builder import build_review_envelope

NOW = datetime(2026, 9, 1, 21, 0, tzinfo=UTC)
TRADE_DATE = date(2026, 9, 1)


def _control_payload(artifact_id: str) -> dict[str, object]:
    return {
        "id": artifact_id,
        "market_scope": "US-equity",
        "status": "active",
        "mode": "PAPER_ONLY",
        "metadata": {
            "allow_order_execution": False,
            "production_eligible": False,
        },
    }


def _control_hash(artifact_id: str) -> str:
    return config_sha256(_control_payload(artifact_id))


def _binding() -> LoopBinding:
    return LoopBinding(
        signal_contract_id="signal-v1",
        signal_contract_sha256=_control_hash("signal-v1"),
        fsm_contract_id="fsm-v1",
        fsm_contract_sha256=_control_hash("fsm-v1"),
        fsm_review_event_type="review_completed",
        golden_suite_id="golden-v1",
        golden_suite_sha256=_control_hash("golden-v1"),
        golden_actual_results={"paper-only": {"verdict": "PAPER_ONLY"}},
    )


def _control_artifact(
    artifact_id: str,
    artifact_type: str,
    *,
    available_at: datetime = NOW,
    config_hash: str | None = None,
) -> dict[str, object]:
    payload = _control_payload(artifact_id)
    payload["metadata"]["config_sha256"] = config_hash or _control_hash(artifact_id)  # type: ignore[index]
    return {
        "id": artifact_id,
        "artifact_type": artifact_type,
        "market_scope": "US-equity",
        "status": "active",
        "effective_at": available_at.isoformat(),
        "available_at": available_at.isoformat(),
        "payload": payload,
    }


def _opportunity(tmp_path: Path) -> tuple[Path, object]:
    rows = []
    cutoff = datetime(2026, 9, 1, 13, 25, tzinfo=UTC)
    for index in range(12):
        selected = index < 3
        rows.append(
            {
                "session_date": TRADE_DATE,
                "selection_cutoff_utc": cutoff,
                "opportunity_rank": index + 1,
                "symbol": f"T{index:02d}",
                "selection_status": "selected" if selected else "rejected",
                "root_cause": "selected" if selected else "intentional_gate",
                "root_cause_detail": "selected by frozen gate" if selected else "RVOL gate",
                "pattern_key": "selected" if selected else "intentional_gate:rvol",
                "close_return": 0.02 - index / 1000,
                "mfe_from_previous_close": 0.03,
                "mae_from_previous_close": -0.01,
                "dollar_volume": 2_000_000.0,
                "atr_pct": 0.04,
                "provenance": "accepted.test",
            }
        )
    snapshot, path = persist_snapshot(
        pl.DataFrame(rows),
        root=tmp_path / "data",
        source="research.intraday_selection_postmortem",
        schema_version="intraday_selection_postmortem.v1",
        checks=(
            DataQualityCheck(
                name="complete",
                severity=QualitySeverity.CRITICAL,
                passed=True,
                observed="12",
                expected=">=10",
                provenance="test",
            ),
        ),
    )
    return path, snapshot


def _envelope(tmp_path: Path):
    path, snapshot = _opportunity(tmp_path)
    active = build_strategy_policy(
        version="selection-v1",
        status="active",
        min_rvol=3.0,
        created_at_utc=NOW,
        approved_by="owner",
        approved_at_utc=NOW,
    )
    return build_review_envelope(
        project_root=Path(__file__).resolve().parents[1],
        trade_date=TRADE_DATE,
        opportunity_path=path,
        opportunity_snapshot=snapshot,  # type: ignore[arg-type]
        artifact_ids=("episode-1", "review-1"),
        cfg=load_config(Path(__file__).resolve().parents[1] / "config.yaml"),
        active_policy=active,
    )


def test_review_builder_keeps_top10_separate_and_never_fabricates_paths(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    assert len(envelope.top10_decisions) == 10
    assert sum(item.verdict == "accept" for item in envelope.top10_decisions) == 3
    assert all(item.one_minute_path == () for item in envelope.top10_decisions)
    assert envelope.execution_summary["orders_authorized"] is False
    task = build_loop_task(envelope, _binding())
    assert task["workflow_version_id"] == "workflow-version-quant-daily-review-v5"
    assert len(task["input_data"]["dynamic_rescan"]["ranked_candidates"]) == 10
    assert task["input_data"]["daily_review"]["outcome_ids"] == []
    assert task["constraints"]["allow_order_execution"] is False


def test_loop_client_creates_idempotent_task_then_runs_it(tmp_path: Path) -> None:
    calls: list[tuple[str, str, object]] = []

    def request(method: str, path: str, payload: object) -> object:
        calls.append((method, path, payload))
        if path.startswith("/api/v1/knowledge/quant/control-artifacts?"):
            artifact_type = path.split("artifact_type=", 1)[1].split("&", 1)[0]
            artifact_id = {
                "signal_contract": "signal-v1",
                "fsm_contract": "fsm-v1",
                "golden_case_suite": "golden-v1",
            }[artifact_type]
            return [_control_artifact(artifact_id, artifact_type)]
        if path == "/api/v1/tasks":
            return {"id": "task-1"}
        return {"id": "run-1", "status": "COMPLETED"}

    client = LoopClient(base_url="https://loop.invalid", api_key="secret", request=request)
    assert client.submit_review(_envelope(tmp_path), _binding()) == ("task-1", "run-1")
    assert all(
        item[1].startswith("/api/v1/knowledge/quant/control-artifacts?") for item in calls[:3]
    )
    assert [item[1] for item in calls[-2:]] == [
        "/api/v1/tasks",
        "/api/v1/tasks/task-1/run",
    ]


def test_missing_contract_blocks_before_remote_task_creation(tmp_path: Path) -> None:
    calls: list[str] = []

    def request(method: str, path: str, payload: object) -> object:
        del method, payload
        calls.append(path)
        return []

    client = LoopClient(base_url="https://loop.invalid", api_key="secret", request=request)
    with pytest.raises(LoopPreconditionError) as caught:
        client.submit_review(_envelope(tmp_path), _binding())
    assert caught.value.code == "CONTRACT_NOT_FOUND"
    assert not any(path == "/api/v1/tasks" for path in calls)
    outbox = LoopOutbox(tmp_path / "blocked.sqlite3")
    outbox.stage(
        event_id="review-blocked",
        event_type="daily_review",
        payload={"safe": True},
        payload_sha256=hashlib.sha256(b"blocked").hexdigest(),
    )
    outbox.mark_blocked_precondition("review-blocked", error_code=caught.value.code)
    blocked = outbox.get("review-blocked")
    assert blocked is not None
    assert blocked.status == "blocked_precondition"
    assert blocked.remote_task_id is None and blocked.remote_run_id is None


def test_review_before_contract_available_at_is_audit_only(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    future = envelope.as_of.replace(year=envelope.as_of.year + 1)

    def request(method: str, path: str, payload: object) -> object:
        del method, payload
        artifact_type = path.split("artifact_type=", 1)[1].split("&", 1)[0]
        artifact_id = {
            "signal_contract": "signal-v1",
            "fsm_contract": "fsm-v1",
            "golden_case_suite": "golden-v1",
        }[artifact_type]
        return [_control_artifact(artifact_id, artifact_type, available_at=future)]

    client = LoopClient(base_url="https://loop.invalid", api_key="secret", request=request)
    with pytest.raises(AuditOnlyBackfillRequired) as caught:
        client.submit_review(envelope, _binding())
    assert caught.value.code == "CONTRACT_NOT_AVAILABLE_AT_AS_OF"


def test_http_200_failed_run_preserves_remote_failure_evidence(tmp_path: Path) -> None:
    def request(method: str, path: str, payload: object) -> object:
        del method, payload
        if path.startswith("/api/v1/knowledge/quant/control-artifacts?"):
            artifact_type = path.split("artifact_type=", 1)[1].split("&", 1)[0]
            artifact_id = {
                "signal_contract": "signal-v1",
                "fsm_contract": "fsm-v1",
                "golden_case_suite": "golden-v1",
            }[artifact_type]
            return [_control_artifact(artifact_id, artifact_type)]
        if path == "/api/v1/tasks":
            return {"id": "task-1"}
        return {
            "id": "run-1",
            "status": "FAILED",
            "events": [
                {
                    "event": "quant_step_failed",
                    "step_id": "golden_replay",
                    "error_type": "GoldenReplayMismatch",
                }
            ],
        }

    client = LoopClient(base_url="https://loop.invalid", api_key="secret", request=request)
    with pytest.raises(LoopRunFailedError) as caught:
        client.submit_review(_envelope(tmp_path), _binding())
    outbox = LoopOutbox(tmp_path / "failed.sqlite3")
    envelope = _envelope(tmp_path)
    outbox.stage(
        event_id=envelope.event_id,
        event_type="daily_review",
        payload=envelope.model_dump(mode="json"),
        payload_sha256=envelope.payload_sha256,
    )
    failure = caught.value
    outbox.mark_failed(
        envelope.event_id,
        error_code=failure.error_code,
        remote_task_id=failure.task_id,
        remote_run_id=failure.run_id,
        failed_node=failure.failed_node,
    )
    item = outbox.get(envelope.event_id)
    assert item is not None
    assert (item.status, item.remote_task_id, item.remote_run_id) == (
        "failed",
        "task-1",
        "run-1",
    )
    assert (item.failed_node, item.last_error_code) == (
        "golden_replay",
        "GoldenReplayMismatch",
    )


def test_explicit_control_plane_initialization_is_idempotent() -> None:
    manifest_path = (
        Path(__file__).resolve().parents[1] / "config/loop_control_plane/us_equity.v1.json"
    )
    manifest = LoopControlPlaneManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    stored: dict[str, dict[str, object]] = {}
    post_calls: list[str] = []

    def request(method: str, path: str, payload: object) -> object:
        if method == "GET":
            artifact_type = path.split("artifact_type=", 1)[1].split("&", 1)[0]
            return [item for item in stored.values() if item["artifact_type"] == artifact_type]
        assert isinstance(payload, dict)
        post_calls.append(path)
        artifact_type = {
            value: key
            for key, value in {
                "signal_contract": "/api/v1/knowledge/quant/signal-contracts",
                "fsm_contract": "/api/v1/knowledge/quant/fsm-contracts",
                "golden_case_suite": "/api/v1/knowledge/quant/golden-suites",
            }.items()
        }[path]
        artifact = {
            "id": payload["id"],
            "artifact_type": artifact_type,
            "market_scope": payload["market_scope"],
            "status": payload["status"],
            "effective_at": payload["effective_at"],
            "available_at": payload["available_at"],
            "payload": payload,
        }
        stored[str(payload["id"])] = artifact
        return artifact

    client = LoopClient(base_url="https://loop.invalid", api_key="secret", request=request)
    first = client.initialize_control_plane(manifest)
    second = client.initialize_control_plane(manifest)
    assert first == second == manifest.binding()
    assert len(post_calls) == 3


def test_complete_review_keeps_active_policy_immutable_and_never_calls_broker(
    tmp_path: Path,
) -> None:
    active_path = tmp_path / "active.json"
    active = build_strategy_policy(
        version="selection-v1",
        status="active",
        min_rvol=3.0,
        created_at_utc=NOW,
        approved_by="owner",
        approved_at_utc=NOW,
    )
    write_strategy_policy(active_path, active)
    before = hashlib.sha256(active_path.read_bytes()).hexdigest()
    calls: list[str] = []

    def request(method: str, path: str, payload: object) -> object:
        del method, payload
        calls.append(path)
        if path.startswith("/api/v1/knowledge/quant/control-artifacts?"):
            artifact_type = path.split("artifact_type=", 1)[1].split("&", 1)[0]
            artifact_id = {
                "signal_contract": "signal-v1",
                "fsm_contract": "fsm-v1",
                "golden_case_suite": "golden-v1",
            }[artifact_type]
            return [_control_artifact(artifact_id, artifact_type)]
        if path == "/api/v1/tasks":
            return {"id": "task-1"}
        return {"id": "run-1", "status": "COMPLETED"}

    result = LoopClient(
        base_url="https://loop.invalid", api_key="secret", request=request
    ).submit_review(_envelope(tmp_path), _binding())
    after = hashlib.sha256(active_path.read_bytes()).hexdigest()
    assert result == ("task-1", "run-1")
    assert before == after
    assert load_strategy_policy(active_path).policy_hash == active.policy_hash
    assert not any("broker" in path.lower() or "oms" in path.lower() for path in calls)


def test_outbox_rejects_identity_collision_and_tracks_remote_ids(tmp_path: Path) -> None:
    outbox = LoopOutbox(tmp_path / "outbox.sqlite3")
    outbox.stage(
        event_id="review-1",
        event_type="daily_review",
        payload={"a": 1},
        payload_sha256=hashlib.sha256(b"one").hexdigest(),
    )
    with pytest.raises(ValueError, match="collided"):
        outbox.stage(
            event_id="review-1",
            event_type="daily_review",
            payload={"a": 2},
            payload_sha256=hashlib.sha256(b"two").hexdigest(),
        )
    outbox.mark_delivered("review-1", remote_task_id="task-1", remote_run_id="run-1")
    item = outbox.get("review-1")
    assert item is not None
    assert item.status == "delivered"
    assert item.remote_task_id == "task-1"
    assert item.remote_run_id == "run-1"


def _candidate(*, trading_policy: dict[str, object] | None = None) -> LoopPolicyCandidate:
    return LoopPolicyCandidate.model_validate(
        {
            "id": "artifact-1",
            "artifact_type": "strategy_policy_candidate",
            "market_scope": "US-equity",
            "status": "candidate",
            "effective_at": NOW.isoformat(),
            "available_at": NOW.isoformat(),
            "source_run_id": "run-1",
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "payload": {
                "schema_version": "quant-strategy-policy-v3",
                "mode": "PAPER_ONLY",
                "strategy_revision_id": "revision-1",
                "strategy_fingerprint": "f" * 64,
                "selection_policy": {"parameter_overrides": {"universe.min_rvol": 3.5}},
                "trading_policy": trading_policy or {},
                "production_eligible": False,
                "allow_order_execution": False,
            },
        }
    )


def test_loop_candidate_can_only_install_allowlisted_shadow_policy(tmp_path: Path) -> None:
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
        _candidate(),
        active_path=active_path,
        challenger_path=challenger_path,
        installed_at_utc=NOW,
    )
    assert challenger.status == "shadow"
    assert challenger.min_rvol == 3.5
    assert challenger.previous_version == active.version
    assert active_path.read_text() == active.model_dump_json(indent=2)
    with pytest.raises(ValueError, match="trading policy"):
        install_shadow_candidate(
            _candidate(trading_policy={"stop_loss": 0.5}),
            active_path=active_path,
            challenger_path=tmp_path / "other.json",
        )


def test_delayed_outcome_requires_revision_and_point_in_time_lineage() -> None:
    with pytest.raises(ValueError, match="strategy_revision_id"):
        LoopOutcomeEnvelope(
            id="outcome-1",
            decision_event_id="event-1",
            source_run_id="run-1",
            market_scope="US-equity",
            instrument="AAPL",
            horizon="1d",
            observed_at=NOW,
            evidence={"snapshot_id": "snapshot-1"},
            metadata={
                "point_in_time_guard_passed": True,
                "evaluation_role": "forward",
            },
        )
