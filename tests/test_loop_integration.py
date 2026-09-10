from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from test_loop_execution import fill, fills_payload, native_plan_payload, pinned_json, plan_payload

from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.daily import audit_daily_bars, canonicalize_daily_bars
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
    validate_loop_task_cohort,
)
from operations.loop_integration.contracts import (
    LoopBinding,
    LoopOutcomeAssignment,
    LoopOutcomeEnvelope,
    LoopPolicyCandidate,
    OutcomeReporterConfig,
    QuantReviewEnvelope,
)
from operations.loop_integration.control_plane import (
    LoopControlPlaneManifest,
    config_sha256,
)
from operations.loop_integration.outbox import LoopOutbox
from operations.loop_integration.outcome_reporter import sync_due_outcomes
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


def _opportunity(tmp_path: Path) -> tuple[Path, DatasetSnapshot]:
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
                "classification": "SELECTED" if selected else "INTENTIONAL_GATE",
                "classification_source": "intraday_selection_postmortem.rule_classifier.v1",
                "logging_policy_id": "kernel.universe.selection_gates.v2@test",
                "logged_action": "accept" if selected else "reject",
                "logging_action_probability": 1.0,
                "reward_model_logged": 0.0,
                "pattern_key": "selected" if selected else "intentional_gate:rvol",
                "rvol": 4.0 if selected else 2.5,
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


def _envelope(
    tmp_path: Path, *, with_plan: bool = True, plan_overrides: dict[str, Any] | None = None,
    fill_rows: list[dict[str, Any]] | None = None,
    native_risk: bool = True,
) -> QuantReviewEnvelope:
    path, snapshot = _opportunity(tmp_path)
    active = build_strategy_policy(
        version="selection-v1",
        status="active",
        min_rvol=3.0,
        created_at_utc=NOW,
        approved_by="owner",
        approved_at_utc=NOW,
    )
    plan_path = tmp_path / "effective-plan.json"
    plan_data = {**plan_payload(active.policy_hash), **(plan_overrides or {})}
    context_path = tmp_path / "context.json"
    context_hash = None
    plan_hash: str | None
    if with_plan and native_risk:
        native = native_plan_payload()
        native.pop("loop_review")
        plan_hash = pinned_json(plan_path, native)
        plan_data["native_plan_sha256"] = plan_hash
        context_hash = pinned_json(context_path, plan_data)
    else:
        plan_hash = pinned_json(plan_path, plan_data) if with_plan else None
    fill_path = tmp_path / "fills.json"
    fill_hash = None
    if fill_rows is not None:
        assert plan_hash is not None
        evidence = fills_payload(plan_hash, fill_rows)
        evidence["strategy_sha256"] = plan_data["strategy_sha256"]
        fill_hash = pinned_json(fill_path, evidence)
    return build_review_envelope(
        project_root=Path(__file__).resolve().parents[1],
        trade_date=TRADE_DATE,
        opportunity_path=path,
        opportunity_snapshot=snapshot,
        artifact_ids=("episode-1", "review-1"),
        cfg=load_config(Path(__file__).resolve().parents[1] / "config.yaml"),
        active_policy=active,
        effective_plan_path=plan_path if with_plan else None,
        effective_plan_sha256=plan_hash,
        fill_evidence_path=fill_path if fill_rows is not None else None,
        fill_evidence_sha256=fill_hash,
        review_context_path=context_path if context_hash else None,
        review_context_sha256=context_hash,
    )


def test_review_builder_keeps_top10_separate_and_never_fabricates_paths(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    assert envelope.provenance.created_at_utc == envelope.as_of
    assert len(envelope.top10_decisions) == 10
    assert sum(item.verdict == "accept" for item in envelope.top10_decisions) == 3
    assert all(item.one_minute_path == () for item in envelope.top10_decisions)
    assert all(item.market_regime == "UNKNOWN" for item in envelope.top10_decisions)
    assert all(item.classification for item in envelope.top10_decisions)
    assert all(item.classification_source for item in envelope.top10_decisions)
    assert all(item.logging_action_probability == 1.0 for item in envelope.top10_decisions)
    assert all(
        item.logged_action in {"accept", "watch", "reject", "block"}
        for item in envelope.top10_decisions
    )
    assert envelope.execution_summary["orders_authorized"] is False
    task = build_loop_task(envelope, _binding())
    primary = envelope.top10_decisions[0]
    assert task["workflow_version_id"] == "workflow-version-quant-daily-review-v6"
    assert task["input_data"]["dynamic_rescan"]["universe"] == [
        f"T{index:02d}" for index in range(10)
    ]
    assert len(task["input_data"]["dynamic_rescan"]["ranked_candidates"]) == 10
    assert task["input_data"]["daily_review"]["outcome_ids"] == []
    review = task["input_data"]["daily_review"]
    assert "top10_pnl" not in review["metrics"]
    assert review["metrics"]["top10_close_return_sum"] == pytest.approx(0.155)
    assert review["metrics"]["non_top10_close_return_sum"] == pytest.approx(0.019)
    assert review["metrics"]["top10_positive_close_return_rate"] == 1.0
    assert review["metrics"]["top10_close_return_sample_count"] == 10
    assert review["metrics"]["non_top10_close_return_sample_count"] == 2
    assert review["metric_semantics"] == {
        "schema_version": "quant-review-metrics-v2",
        "return_unit": "decimal_fraction",
        "return_aggregation": "unweighted_sum_of_instrument_close_returns",
        "positive_rate_denominator": "top10_instruments_with_close_return",
        "portfolio_pnl_available": False,
    }
    fsm_transition = task["input_data"]["fsm_transition"]
    assert {
        key: fsm_transition[key]
        for key in (
            "contract_id",
            "market_scope",
            "instrument",
            "event_type",
            "event_time",
            "available_at",
            "as_of",
            "reason",
        )
    } == {
        "contract_id": "fsm-v1",
        "market_scope": "US-equity",
        "instrument": primary.instrument,
        "event_type": "review_completed",
        "event_time": primary.event_time.isoformat(),
        "available_at": primary.available_at.isoformat(),
        "as_of": envelope.as_of.isoformat(),
        "reason": "daily_review_completed",
    }
    assert fsm_transition["guard_snapshot"]["orders_authorized"] is False
    assert fsm_transition["metadata"]["source_system"] == "ai-quant-trading-system"
    assert task["constraints"]["allow_order_execution"] is False
    ranked = [
        item["instrument"]
        for item in task["input_data"]["dynamic_rescan"]["ranked_candidates"][:10]
    ]
    adjudicated = [
        item["instrument"]
        for item in task["input_data"]["top10_adjudication"]["decisions"]
    ]
    reviewed = [
        item["instrument"]
        for item in task["input_data"]["daily_review"]["top10_verdicts"]
    ]
    assert ranked == adjudicated == reviewed


def test_client_rejects_a_mixed_top10_cohort_before_remote_submission(
    tmp_path: Path,
) -> None:
    task = build_loop_task(_envelope(tmp_path), _binding())
    task["input_data"]["top10_adjudication"]["decisions"][0]["instrument"] = "OTHER"

    with pytest.raises(
        ValueError,
        match=r"Top10 cohort mismatch.*missing=T00.*unexpected=OTHER",
    ):
        validate_loop_task_cohort(task)


def test_review_without_effective_plan_does_not_invent_risk_or_execution(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path, with_plan=False)
    assert envelope.risk_policy["status"] == "unavailable"
    assert "stop_loss" not in envelope.risk_policy
    assert envelope.execution_summary["status"] == "unavailable"
    assert envelope.execution_summary["realized_net_pnl"] is None
    assert envelope.market_context["frozen_candidate_pool"]["count"] is None
    assert envelope.market_context["post_close_winners"]["count"] == 12

    def no_request(method: str, path: str, payload: object) -> object:
        pytest.fail("missing effective risk must block before any network request")

    with pytest.raises(LoopPreconditionError) as caught:
        client = LoopClient(base_url="https://loop.invalid", api_key="test", request=no_request)
        client.submit_review(envelope, _binding())
    assert caught.value.code == "EFFECTIVE_MODERN_PLAN_UNAVAILABLE"


def test_review_uses_effective_modern_risk_and_separates_morning_pool(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    risk = envelope.risk_policy
    assert risk["status"] == "available"
    assert risk["position_limits"]["risk_per_trade_fraction"] == 0.005
    assert risk["stop_loss"]["threshold_pct"] == 2.0
    assert risk["exit_conditions"] == ["no_new_entry_et=15:00", "flatten_et=15:50"]
    assert risk["attempt_weights"] == [0.6, 0.4]
    assert risk["evidence"]["plan_sha256"] == hashlib.sha256(
        (tmp_path / "effective-plan.json").read_bytes()
    ).hexdigest()
    assert "ATR" not in str(risk) and "15:55" not in str(risk)
    assert envelope.market_context["frozen_candidate_pool"]["count"] == 2
    task = build_loop_task(envelope, _binding())
    review = task["input_data"]["daily_review"]
    assert review["frozen_candidate_pool"]["candidates"][0]["symbol"] == "MORNING"
    assert review["post_close_winners"]["count"] == 12
    assert review["execution_summary"]["status"] == "unavailable"
    assert "selected_return" not in review["metrics"]


def test_review_carries_factual_summary_and_full_pool_without_winner_selection_bias(
    tmp_path: Path,
) -> None:
    candidates = [{"symbol": f"POOL{index}", "verdict": "reject"} for index in range(15)]
    candidates[0] = {"symbol": "MORNING", "verdict": "accept"}
    envelope = _envelope(
        tmp_path, plan_overrides={"candidates": candidates},
        fill_rows=[fill("buy-1", "buy", "4", "100", 14),
                   fill("sell-1", "sell", "4", "120", 15)],
    )
    task = build_loop_task(envelope, _binding())
    review = task["input_data"]["daily_review"]
    assert review["frozen_candidate_pool"]["count"] == 15
    assert len(task["input_data"]["dynamic_rescan"]["ranked_candidates"]) == 10
    assert len(review["frozen_candidate_pool"]["candidates"]) == 15
    assert review["execution_summary"]["realized_net_pnl"] == 78
    assert review["execution_summary"]["strategy_sha256"] == (
        review["risk_policy"]["evidence"]["strategy_sha256"]
    )
    assert review["metric_semantics"]["portfolio_pnl_available"] is False
    assert review["metrics"]["top10_close_return_sum"] == pytest.approx(0.155)


def test_missing_morning_pool_never_falls_back_to_winners(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path, plan_overrides={
        "candidate_pool_complete": False, "candidates": None,
    })
    task = build_loop_task(envelope, _binding())
    assert task["input_data"]["dynamic_rescan"]["universe"] == [
        f"T{index:02d}" for index in range(10)
    ]
    assert task["input_data"]["daily_review"]["frozen_candidate_pool"]["count"] is None
    assert len(envelope.top10_decisions) == 10


def test_review_rejects_active_strategy_config_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="strategy/config"):
        _envelope(tmp_path, plan_overrides=plan_payload("b" * 64))


def test_review_sidecar_without_native_facts_is_risk_unavailable(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path, native_risk=False)
    assert envelope.risk_policy["status"] == "unavailable"
    assert "position_limits" not in envelope.risk_policy


@pytest.mark.parametrize("with_risk", [False, True])
@pytest.mark.parametrize("use_index", [False, True])
def test_daily_review_cli_stages_native_plan_end_to_end_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    with_risk: bool, use_index: bool,
) -> None:
    from scripts import sync_loop_daily_review

    _opportunity(tmp_path)
    active = build_strategy_policy(
        version="selection-v1", status="active", min_rvol=3.0,
        created_at_utc=NOW, approved_by="owner", approved_at_utc=NOW,
    )
    active_path = tmp_path / "active.json"
    write_strategy_policy(active_path, active)
    binding_path = tmp_path / "binding.json"
    pinned_json(binding_path, _binding().model_dump(mode="json"))
    args = [
        "sync_loop_daily_review", "--trade-date", "2026-09-01",
        "--binding", str(binding_path), "--active-policy", str(active_path),
        "--data-root", str(tmp_path / "data"), "--outbox", str(tmp_path / "outbox.sqlite3"),
        "--stage-only",
    ]
    inputs: list[Path] = [active_path, binding_path]
    if with_risk:
        native = native_plan_payload()
        native.pop("loop_review")
        plan_path = tmp_path / "modern_h15_paper_plan.json"
        plan_hash = pinned_json(plan_path, native)
        context = plan_payload(active.policy_hash)
        context.update(
            effective_at_utc="2026-09-01T13:35:00Z", available_at_utc="2026-09-01T13:35:00Z",
            native_plan_sha256=plan_hash,
        )
        context_path = tmp_path / "review-context.json"
        context_hash = pinned_json(context_path, context)
        evidence = fills_payload(plan_hash, [])
        evidence["strategy_sha256"] = context["strategy_sha256"]
        fill_path = tmp_path / "fills.json"
        fill_hash = pinned_json(fill_path, evidence)
        direct_args = [
            "--effective-plan", str(plan_path), "--effective-plan-sha256", plan_hash,
            "--review-context", str(context_path), "--review-context-sha256", context_hash,
            "--fill-evidence", str(fill_path), "--fill-evidence-sha256", fill_hash,
        ]
        if use_index:
            index_path = tmp_path / "execution-index.json"
            index_hash = pinned_json(index_path, {"executions": [{
                "trade_date": "2026-09-01", "strategy_sha256": context["strategy_sha256"],
                "plan_path": str(plan_path), "plan_sha256": plan_hash,
                "fills_path": str(fill_path), "fills_sha256": fill_hash,
                "review_context_path": str(context_path), "review_context_sha256": context_hash,
            }]})
            args += ["--execution-index", str(index_path), "--execution-index-sha256", index_hash]
            inputs.append(index_path)
        else:
            args += direct_args
        inputs += [plan_path, context_path, fill_path]
    before = {str(path): path.read_bytes() for path in inputs}
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(sync_loop_daily_review, "load_project_env", lambda root: None)
    monkeypatch.setattr(
        sync_loop_daily_review, "LoopClient",
        lambda **kwargs: pytest.fail("stage-only must not initialize any remote client"),
    )
    sync_loop_daily_review.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "staged"
    item = LoopOutbox(tmp_path / "outbox.sqlite3").get(result["event_id"])
    assert item is not None
    assert item.payload["risk_policy"]["status"] == ("available" if with_risk else "unavailable")
    if with_risk:
        assert item.payload["execution_summary"]["status"] == "no_trade"
        risk_evidence = item.payload["risk_policy"]["evidence"]
        assert risk_evidence["available_at"] == "2026-09-01T13:35:00+00:00"
    assert {str(path): path.read_bytes() for path in inputs} == before


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


def _candidate(
    *,
    forbidden_execution_fields: dict[str, object] | None = None,
    production_eligible: bool = False,
) -> LoopPolicyCandidate:
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
                "schema_version": "strategy_policy_candidate.v4",
                "mode": "PAPER_ONLY",
                "strategy_revision_id": "revision-1",
                "fingerprint": "f" * 64,
                "advisory_rule": {"selection_policy": {"decision": "watch"}},
                "allowed_parameter_overrides": {"universe.min_rvol": 3.5},
                "forbidden_execution_fields": forbidden_execution_fields or {},
                "production_eligible": production_eligible,
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
    filtered = install_shadow_candidate(
        _candidate(
            forbidden_execution_fields={
                "trading_policy": {"stop_loss": 0.5},
                "broker_account": "forbidden",
            }
        ),
        active_path=active_path,
        challenger_path=tmp_path / "filtered.json",
    )
    assert filtered.min_rvol == 3.5
    assert filtered.status == "shadow"
    with pytest.raises(ValueError, match="production eligibility"):
        _candidate(production_eligible=True)


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


def test_outcome_v1_moves_legacy_lineage_to_canonical_evidence() -> None:
    outcome = LoopOutcomeEnvelope(
        id="outcome-v1",
        decision_event_id="event-1",
        source_run_id="run-1",
        market_scope="US-equity",
        instrument="AAPL",
        horizon="1d",
        observed_at=NOW,
        evidence={"snapshot_id": "snapshot-1"},
        metadata={
            "strategy_revision_id": "strategy-r1",
            "evaluation_role": "forward",
            "point_in_time_guard_passed": True,
        },
    )

    assert outcome.evidence["strategy_revision_id"] == "strategy-r1"
    assert outcome.evidence["evaluation_role"] == "forward"


def test_outcome_v2_enforces_maturity_and_return_semantics() -> None:
    evidence = {
        "schema_version": "quant-outcome-evidence-v2",
        "strategy_revision_id": "strategy-r1",
        "evaluation_role": "holdout",
        "point_in_time_guard_passed": True,
        "decision_trading_date": "2026-08-31",
        "horizon_end_trading_date": "2026-09-01",
        "horizon_end_market_close_utc": "2026-09-01T20:00:00+00:00",
        "trading_session_dates": ["2026-09-01"],
        "trading_calendar": {
            "name": "XNYS",
            "source": "pandas_market_calendars.NYSE",
            "version": "5.1.1",
        },
        "benchmark_id": "QQQ",
        "price_snapshot_ids": ["snapshot-start", "snapshot-end"],
        "return_semantics": {
            "unit": "decimal_fraction",
            "method": "close_to_close_split_adjusted",
            "strategy_return_basis": "gross_before_costs",
            "excess_return_formula": "strategy_return-benchmark_return-transaction_cost-slippage",
        },
    }
    outcome = LoopOutcomeEnvelope(
        schema_version="ai_quant.loop_outcome.v2",
        id="outcome-v2",
        decision_event_id="event-1",
        source_run_id="run-1",
        market_scope="US-equity",
        instrument="AAPL",
        horizon="1d",
        observed_at=NOW,
        strategy_return=0.03,
        benchmark_return=0.01,
        excess_return=0.018,
        max_drawdown=-0.01,
        transaction_cost=0.001,
        slippage=0.001,
        direction_correct=True,
        evidence=evidence,
        metadata={"source_system": "ai-quant-trading-system"},
    )
    assert outcome.excess_return == pytest.approx(0.018)

    with pytest.raises(ValueError, match="cannot precede horizon close"):
        LoopOutcomeEnvelope.model_validate(
            {
                **outcome.model_dump(mode="json"),
                "id": "outcome-v2-early",
                "observed_at": "2026-09-01T19:59:00+00:00",
            }
        )


def test_outcome_v2_keeps_legacy_and_counterfactual_costs_separate() -> None:
    evidence = {
        "schema_version": "quant-outcome-evidence-v2",
        "strategy_revision_id": "strategy-r1",
        "evaluation_role": "holdout",
        "point_in_time_guard_passed": True,
        "decision_trading_date": "2026-08-31",
        "horizon_end_trading_date": "2026-09-01",
        "horizon_end_market_close_utc": "2026-09-01T20:00:00+00:00",
        "trading_session_dates": ["2026-09-01"],
        "trading_calendar": {
            "name": "XNYS",
            "source": "pandas_market_calendars.NYSE",
            "version": "5.1.1",
        },
        "benchmark_id": "QQQ",
        "price_snapshot_ids": ["snapshot-start", "snapshot-end"],
        "return_semantics": {
            "unit": "decimal_fraction",
            "method": "close_to_close_split_adjusted",
            "strategy_return_basis": "gross_before_costs",
            "excess_return_formula": "strategy_return-benchmark_return-transaction_cost-slippage",
        },
        "counterfactual_transaction_cost": 0.001,
        "counterfactual_slippage": 0.0005,
    }
    outcome = LoopOutcomeEnvelope(
        schema_version="ai_quant.loop_outcome.v2",
        id="outcome-v3-watch",
        decision_event_id="event-1",
        source_run_id="run-1",
        market_scope="US-equity",
        instrument="AAPL",
        horizon="1d",
        observed_at=NOW,
        instrument_return=0.04,
        strategy_return=0.0,
        counterfactual_instrument_return=0.04,
        counterfactual_net_excess_return=0.0285,
        benchmark_return=0.01,
        excess_return=-0.01,
        max_drawdown=0.0,
        transaction_cost=0.0,
        slippage=0.0,
        direction_correct=False,
        evidence=evidence,
        metadata={"source_system": "ai-quant-trading-system"},
    )
    assert outcome.excess_return == pytest.approx(-0.01)
    assert outcome.counterfactual_net_excess_return == pytest.approx(0.0285)


def test_outcome_assignment_and_reporter_config_are_fail_closed() -> None:
    assignment = LoopOutcomeAssignment(
        strategy_revision_id="strategy-r1",
        strategy_lineage_id="strategy-lineage-1",
        decision_event_id="event-1",
        source_run_id="run-1",
        market_scope="US-equity",
        instrument="AAPL",
        decision_trading_date=date(2026, 8, 31),
        observed_verdict="watch",
        target_verdict="accept",
        evaluation_role="holdout",
        outstanding_horizons=("1d", "5d", "20d"),
    )
    assert assignment.target_verdict == "accept"

    with pytest.raises(ValueError, match="approved_by"):
        OutcomeReporterConfig(
            benchmark_symbol="QQQ",
            transaction_cost_bps_round_trip=10,
            slippage_bps_round_trip=5,
            cost_model_version="cost-v1",
            approved_by="",
            approved_at_utc=NOW,
        )


@pytest.mark.parametrize("with_execution", [False, True])
def test_due_outcome_reporter_waits_for_sessions_then_submits_v2(
    tmp_path: Path, with_execution: bool,
) -> None:
    data_root = tmp_path / "data"
    for trade_date, aapl_close, qqq_close in (
        (date(2026, 8, 31), 100.0, 200.0),
        (date(2026, 9, 1), 104.0, 202.0),
    ):
        frame = canonicalize_daily_bars(
            pl.DataFrame(
                {
                    "symbol": ["AAPL", "QQQ"],
                    "trade_date": [trade_date, trade_date],
                    "provider_ts_utc": [NOW, NOW],
                    "open": [aapl_close - 1, qqq_close - 1],
                    "high": [aapl_close + 1, qqq_close + 1],
                    "low": [aapl_close - 2, qqq_close - 2],
                    "close": [aapl_close, qqq_close],
                    "volume": [1_000_000.0, 2_000_000.0],
                    "trade_count": [10_000, 20_000],
                    "vwap": [aapl_close, qqq_close],
                    "source": ["massive.grouped_daily"] * 2,
                    "feed": ["sip", "sip"],
                    "adjustment": ["split_adjusted", "split_adjusted"],
                }
            )
        )
        persist_snapshot(
            frame,
            root=data_root,
            source="massive.grouped_daily",
            schema_version="bars_daily.v1",
            checks=audit_daily_bars(
                frame,
                provenance="massive.grouped_daily",
                expected_date=trade_date,
            ),
        )
    execution_args: dict[str, Any] = {}
    if with_execution:
        from test_loop_execution import fills_payload, pinned_json, plan_payload

        plan = plan_payload()
        plan["trade_date"] = "2026-08-31"
        for key in ("effective_at_utc", "available_at_utc", "selection_cutoff_utc",
                    "candidate_pool_available_at_utc"):
            plan[key] = plan[key].replace("2026-09-01", "2026-08-31")
        plan["candidates"] = [{"symbol": "AAPL", "verdict": "accept"}]
        plan_path = tmp_path / "plan.json"
        plan_hash = pinned_json(plan_path, plan)
        fills = fills_payload(plan_hash, [])
        fills["trade_date"] = "2026-08-31"
        for key in ("coverage_start_utc", "coverage_end_utc"):
            fills[key] = fills[key].replace("2026-09-01", "2026-08-31")
        fills_path = tmp_path / "fills.json"
        fills_hash = pinned_json(fills_path, fills)
        index = tmp_path / "index.json"
        index_hash = pinned_json(index, {"executions": [{
            "trade_date": "2026-08-31", "strategy_sha256": plan["strategy_sha256"],
            "plan_path": str(plan_path), "plan_sha256": plan_hash,
            "fills_path": str(fills_path), "fills_sha256": fills_hash,
        }]})
        execution_args = {"execution_index_path": index, "execution_index_sha256": index_hash}
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    def request(
        method: str,
        path: str,
        payload: dict[str, object] | None,
    ) -> object:
        requests.append((method, path, payload))
        if method == "GET":
            if "event-outcome-assignments" in path:
                return []
            return [
                {
                    "schema_version": "quant-outcome-assignment-v1",
                    "strategy_revision_id": "strategy-r1",
                    "strategy_sha256": plan["strategy_sha256"] if with_execution else None,
                    "strategy_lineage_id": "strategy-lineage-1",
                    "decision_event_id": "event-1",
                    "source_run_id": "run-1",
                    "market_scope": "US-equity",
                    "instrument": "AAPL",
                    "decision_trading_date": "2026-08-31",
                    "observed_verdict": "watch",
                    "target_verdict": "accept",
                    "evaluation_role": "holdout",
                    "logging_policy_id": "selection-v1@" + "a" * 64,
                    "logged_action": "accept",
                    "logging_action_probability": 1.0,
                    "reward_model_logged": 0.0,
                    "target_policy_id": "strategy-r1",
                    "target_probability_for_logged_action": 1.0,
                    "reward_model_target": 0.0,
                    "outstanding_horizons": ["1d", "5d"],
                }
            ]
        assert payload is not None
        return {"id": payload["id"]}

    config = OutcomeReporterConfig(
        benchmark_symbol="QQQ",
        transaction_cost_bps_round_trip=10,
        slippage_bps_round_trip=5,
        watch_neutral_band_bps=25,
        cost_model_version="approved-cost-v1",
        approved_by="risk-owner",
        approved_at_utc=NOW,
    )
    summary = sync_due_outcomes(
        client=LoopClient(
            base_url="https://loop.example",
            api_key="test",
            request=request,
        ),
        outbox=LoopOutbox(tmp_path / "outbox.sqlite3"),
        data_root=data_root,
        as_of_date=date(2026, 9, 1),
        observed_before=datetime.now(UTC) + timedelta(seconds=1),
        config=config,
        **execution_args,
    )

    assert summary.assignments == 1
    assert summary.event_assignments == 0
    assert summary.strategy_assignments == 1
    assert summary.due == 1
    assert summary.staged == 1
    assert summary.delivered == 1
    assert summary.pending[0].horizon == "5d"
    assert summary.pending[0].reason == "horizon_not_mature"
    posted = next(payload for method, _, payload in requests if method == "POST")
    assert posted is not None
    assert posted["schema_version"] == "ai_quant.loop_outcome.v2"
    expected_id_prefix = "quant_outcome_linked_" if with_execution else "quant_outcome_v3_"
    assert str(posted["id"]).startswith(expected_id_prefix)
    assert posted["strategy_return"] == pytest.approx(0.04)
    assert posted["benchmark_return"] == pytest.approx(0.01)
    assert posted["excess_return"] == pytest.approx(0.0285)
    evidence = posted["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["strategy_revision_id"] == "strategy-r1"
    assert evidence["return_semantics"]["performance_kind"] == "market_counterfactual"
    assert evidence["return_semantics"]["is_realized_trade_pnl"] is False
    assert evidence["counterfactual_selected_close_return"] == pytest.approx(0.04)
    expected_policy_assignment = {
        "logging_policy_id": "selection-v1@" + "a" * 64,
        "logged_action": "accept",
        "target_policy_id": "strategy-r1",
        "logging_action_probability": 1.0,
        "target_probability_for_logged_action": 1.0,
        "reward_model_logged": 0.0,
        "reward_model_target": 0.0,
    }
    assert evidence["policy_assignment"] == expected_policy_assignment
    assert posted["counterfactual_instrument_return"] == pytest.approx(0.04)
    assert posted["counterfactual_net_excess_return"] == pytest.approx(0.0285)
    if with_execution:
        assert evidence["factual_execution"]["status"] == "no_trade"
        assert evidence["factual_execution"]["realized_gross_pnl"] is None
        assert posted["realized_policy_return"] == 0.0
        assert evidence["policy_evaluation"] == {
            **expected_policy_assignment,
            "observed_reward": 0.0,
            "reward_semantics": "realized_policy_return_decimal_fraction",
        }
        assert str(posted["id"]).startswith("quant_outcome_linked_")
        return
    assert posted["realized_policy_return"] is None
    assert "policy_evaluation" not in evidence
    assert evidence["factual_execution"] == {
        "status": "unavailable",
        "reason": "confirmed_fill_evidence_not_supplied",
        "realized_gross_pnl": None,
        "realized_net_pnl": None,
        "fees": None,
    }


def test_due_outcome_reporter_submits_event_observation_without_strategy(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    for trade_date, aapl_close, qqq_close in (
        (date(2026, 9, 3), 100.0, 200.0),
        (date(2026, 9, 4), 104.0, 202.0),
    ):
        frame = canonicalize_daily_bars(
            pl.DataFrame(
                {
                    "symbol": ["AAPL", "QQQ"],
                    "trade_date": [trade_date, trade_date],
                    "provider_ts_utc": [NOW, NOW],
                    "open": [aapl_close - 1, qqq_close - 1],
                    "high": [aapl_close + 1, qqq_close + 1],
                    "low": [aapl_close - 2, qqq_close - 2],
                    "close": [aapl_close, qqq_close],
                    "volume": [1_000_000.0, 2_000_000.0],
                    "trade_count": [10_000, 20_000],
                    "vwap": [aapl_close, qqq_close],
                    "source": ["massive.grouped_daily"] * 2,
                    "feed": ["sip", "sip"],
                    "adjustment": ["split_adjusted", "split_adjusted"],
                }
            )
        )
        persist_snapshot(
            frame,
            root=data_root,
            source="massive.grouped_daily",
            schema_version="bars_daily.v1",
            checks=audit_daily_bars(
                frame,
                provenance="massive.grouped_daily",
                expected_date=trade_date,
            ),
        )
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    def request(
        method: str,
        path: str,
        payload: dict[str, object] | None,
    ) -> object:
        requests.append((method, path, payload))
        if method == "GET" and "event-outcome-assignments" in path:
            return [
                {
                    "schema_version": "quant-event-outcome-assignment-v1",
                    "decision_event_id": "event-raw-1",
                    "source_run_id": "run-raw-1",
                    "market_scope": "US-equity",
                    "instrument": "AAPL",
                    "decision_trading_date": "2026-09-03",
                    "observed_verdict": "block",
                    "outstanding_horizons": ["1d", "5d"],
                }
            ]
        if method == "GET":
            return []
        assert payload is not None
        return {"id": payload["id"]}

    config = OutcomeReporterConfig(
        benchmark_symbol="QQQ",
        transaction_cost_bps_round_trip=10,
        slippage_bps_round_trip=5,
        watch_neutral_band_bps=25,
        cost_model_version="approved-cost-v1",
        approved_by="risk-owner",
        approved_at_utc=NOW,
    )
    summary = sync_due_outcomes(
        client=LoopClient(
            base_url="https://loop.example",
            api_key="test",
            request=request,
        ),
        outbox=LoopOutbox(tmp_path / "outbox.sqlite3"),
        data_root=data_root,
        as_of_date=date(2026, 9, 4),
        observed_before=datetime.now(UTC) + timedelta(seconds=1),
        config=config,
        execution_index_path=tmp_path / "missing-execution-index.json",
        execution_index_sha256="0" * 64,
    )

    assert summary.assignments == 1
    assert summary.event_assignments == 1
    assert summary.strategy_assignments == 0
    assert summary.delivered == 1
    assert summary.pending[0].horizon == "5d"
    posted = next(payload for method, _, payload in requests if method == "POST")
    assert posted is not None
    assert posted["schema_version"] == "ai_quant.loop_event_outcome.v1"
    assert posted["outcome_kind"] == "event_observation"
    assert posted["instrument_return"] == pytest.approx(0.04)
    assert posted["transaction_cost"] == pytest.approx(0.002)
    assert posted["slippage"] == pytest.approx(0.001)
    assert posted["excess_return"] == pytest.approx(0.027)
    assert posted["realized_policy_return"] is None
    assert posted["counterfactual_instrument_return"] == pytest.approx(0.04)
    assert posted["counterfactual_net_excess_return"] == pytest.approx(0.027)
    assert posted["direction_correct"] is False
    assert "strategy_revision_id" not in posted["evidence"]  # type: ignore[operator]
