from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from data_plane.contracts import DataQualityCheck, QualitySeverity
from data_plane.storage import persist_snapshot
from kernel.config import load_config
from kernel.strategy_policy import build_strategy_policy, write_strategy_policy
from operations.loop_integration.client import LoopClient, build_loop_task
from operations.loop_integration.contracts import (
    LoopBinding,
    LoopOutcomeEnvelope,
    LoopPolicyCandidate,
)
from operations.loop_integration.outbox import LoopOutbox
from operations.loop_integration.policy_consumer import install_shadow_candidate
from operations.loop_integration.review_builder import build_review_envelope

NOW = datetime(2026, 9, 1, 21, 0, tzinfo=UTC)
TRADE_DATE = date(2026, 9, 1)


def _binding() -> LoopBinding:
    return LoopBinding(
        signal_contract_id="signal-v1",
        fsm_contract_id="fsm-v1",
        fsm_review_event_type="review_completed",
        golden_suite_id="golden-v1",
        golden_actual_results={"paper-only": {"verdict": "PAPER_ONLY"}},
    )


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
        return {"id": "task-1"} if path == "/api/v1/tasks" else {"id": "run-1"}

    client = LoopClient(base_url="https://loop.invalid", api_key="secret", request=request)
    assert client.submit_review(_envelope(tmp_path), _binding()) == ("task-1", "run-1")
    assert [item[1] for item in calls] == [
        "/api/v1/tasks",
        "/api/v1/tasks/task-1/run",
    ]


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
