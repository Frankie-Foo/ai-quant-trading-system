from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest
from test_loop_execution import fill
from test_loop_integration import NOW, _binding, _envelope, _frozen_candidates

from data_plane.daily import audit_daily_bars, canonicalize_daily_bars
from data_plane.storage import persist_snapshot
from operations.loop_integration.client import (
    LoopClient,
    LoopPreconditionError,
    build_loop_task,
    validate_review_risk_policy_evidence,
)
from operations.loop_integration.contracts import (
    LoopOutcomeEnvelope,
    LoopOutcomeSyncStatus,
    OutcomeReporterConfig,
)
from operations.loop_integration.outbox import LoopOutbox
from operations.loop_integration.outcome_reporter import _stage_and_deliver, sync_due_outcomes


def _no_request(method: str, path: str, payload: object) -> Any:
    pytest.fail(f"local precondition must block all network requests: {method} {path}")


@pytest.mark.parametrize("count", [None, 0, 2, 9])
def test_incompatible_frozen_pool_blocks_build_and_submit_without_winner_backfill(
    tmp_path: Path, count: int | None,
) -> None:
    candidates = None if count is None else _frozen_candidates(count)
    envelope = _envelope(
        tmp_path, fill_rows=[],
        plan_overrides={"candidates": candidates, "candidate_pool_complete": count is not None},
    )
    assert envelope.market_context["frozen_candidate_pool"]["count"] == count
    assert envelope.market_context["post_close_winners"]["count"] == 12
    with pytest.raises(ValueError, match="at least 10 ranked candidates"):
        build_loop_task(envelope, _binding())
    client = LoopClient(base_url="https://loop.invalid", api_key="test", request=_no_request)
    with pytest.raises(LoopPreconditionError) as caught:
        client.submit_review(envelope, _binding())
    assert caught.value.code == "TOP10_COHORT_INCOMPATIBLE"
    assert envelope.market_context["frozen_candidate_pool"]["candidates"] == candidates


def test_unavailable_execution_blocks_before_submit_even_with_valid_risk_and_pool(
    tmp_path: Path,
) -> None:
    envelope = _envelope(tmp_path)
    validate_review_risk_policy_evidence(envelope)
    build_loop_task(envelope, _binding())
    client = LoopClient(base_url="https://loop.invalid", api_key="test", request=_no_request)
    with pytest.raises(LoopPreconditionError) as caught:
        client.submit_review(envelope, _binding())
    assert caught.value.code == "BROKER_EXECUTION_EVIDENCE_UNAVAILABLE"


def test_full_pool_adjudication_uses_frozen_verdicts_and_never_borrows_winner_returns(
    tmp_path: Path,
) -> None:
    candidates = _frozen_candidates(15)
    for index, row in enumerate(candidates):
        row.update(symbol=f"POOL{index}", verdict="reject", logged_action="reject")
    envelope = _envelope(tmp_path, plan_overrides={"candidates": candidates}, fill_rows=[])
    task = build_loop_task(envelope, _binding())["input_data"]
    assert [row["instrument"] for row in task["dynamic_rescan"]["ranked_candidates"]] == [
        row["symbol"] for row in candidates
    ]
    for decision in envelope.top10_decisions:
        assert decision.instrument.startswith("POOL")
        assert decision.verdict == "reject"
        assert decision.decision_intent.action == "avoid"
        assert decision.features["close_return"] is None
        assert decision.features["mfe_from_previous_close"] is None
        assert decision.features["mae_from_previous_close"] is None
        assert decision.source_snapshot_ids == ("frozen-effective-plan", "full-morning-pool")
    assert task["top10_adjudication"]["decisions"] == task["daily_review"]["top10_verdicts"]
    assert envelope.metrics["top10_close_return_sum"] == pytest.approx(0.155)
    assert envelope.execution_summary["status"] == "no_trade"
    assert envelope.execution_summary["realized_net_pnl"] is None


@pytest.mark.parametrize("field", ["verdict", "classification", "logging_action_probability"])
def test_full_pool_missing_decision_evidence_is_not_repaired_from_winners(
    tmp_path: Path, field: str,
) -> None:
    candidates = _frozen_candidates()
    candidates[0].pop(field)
    with pytest.raises(
        ValueError, match="verdict is unavailable|classification contract|OPE contract"
    ):
        _envelope(tmp_path, plan_overrides={"candidates": candidates})


def test_authorization_activation_does_not_rewrite_source_availability(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path, fill_rows=[], plan_overrides={
        "effective_at_utc": "2026-09-01T13:37:00+00:00",
        "available_at_utc": "2026-09-01T13:20:00+00:00",
    })
    validate_review_risk_policy_evidence(envelope)
    evidence = envelope.risk_policy["evidence"]
    assert datetime.fromisoformat(evidence["effective_at"]) == datetime(
        2026, 9, 1, 13, 20, tzinfo=UTC
    )
    assert evidence["available_at"] == "2026-09-01T13:20:00+00:00"
    assert envelope.risk_policy["authorization_effective_at"] == "2026-09-01T13:37:00+00:00"


def test_factual_fill_unknown_fees_stay_null_after_merge(tmp_path: Path) -> None:
    candidates = _frozen_candidates()
    candidates[0]["symbol"] = "MORNING"
    rows = [fill("b", "buy", "4", "100", 14), fill("s", "sell", "4", "120", 15)]
    rows[0].update(fees=None, fee_source=None)
    envelope = _envelope(tmp_path, plan_overrides={"candidates": candidates}, fill_rows=rows)
    review = build_loop_task(envelope, _binding())["input_data"]["daily_review"]
    execution = review["execution_summary"]
    assert execution["status"] == "filled"
    assert execution["realized_gross_pnl"] == 80.0
    assert execution["fees"] is None
    assert execution["realized_net_pnl"] is None


@pytest.mark.parametrize("stage_only", [False, True])
def test_outcome_sync_status_preserves_stage_only_and_idempotent_replay(
    tmp_path: Path, stage_only: bool,
) -> None:
    outcome = LoopOutcomeEnvelope(
        id="synthetic-outcome", decision_event_id="synthetic-decision", source_run_id="test-run",
        market_scope="US-equity", instrument="T00", horizon="1d", observed_at=NOW,
        evidence={
            "strategy_revision_id": "test-revision", "evaluation_role": "forward",
            "point_in_time_guard_passed": True,
        }, metadata={"synthetic": True},
    )
    calls: list[str] = []

    def request(method: str, path: str, payload: object) -> Any:
        assert not stage_only
        calls.append(path)
        return {"id": outcome.id}

    client = LoopClient(base_url="https://loop.invalid", api_key="test", request=request)
    outbox = LoopOutbox(tmp_path / "outcomes.sqlite3")
    statuses: dict[tuple[str, str, str, str], LoopOutcomeSyncStatus] = {}
    for _ in range(2):
        assert _stage_and_deliver(
            [outcome], client=client, outbox=outbox, stage_only=stage_only,
            observed_before=NOW, statuses=statuses,
        ) == (1, 0 if stage_only else 1)
        assert next(iter(statuses.values())).state == ("SYNC_PENDING" if stage_only else "OBSERVED")
    assert len(calls) == (0 if stage_only else 1)
    item = outbox.get(outcome.id)
    assert item is not None
    assert item.payload["realized_policy_return"] is None
    assert item.payload["counterfactual_net_excess_return"] is None


def test_outcome_delivery_error_survives_diagnostic_status_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    outcome = LoopOutcomeEnvelope(
        id="synthetic-outcome", decision_event_id="synthetic-decision", source_run_id="test-run",
        market_scope="US-equity", instrument="T00", horizon="1d", observed_at=NOW,
        evidence={
            "strategy_revision_id": "test-revision", "evaluation_role": "forward",
            "point_in_time_guard_passed": True,
        }, metadata={"synthetic": True},
    )
    calls: list[str] = []

    def request(method: str, path: str, payload: Any) -> Any:
        calls.append(path)
        if path.endswith("outcome-sync-statuses"):
            assert payload["statuses"][0]["state"] == "SYNC_FAILED"
            raise ValueError("diagnostic failure")
        raise RuntimeError("outcome delivery failure")

    client = LoopClient(base_url="https://loop.invalid", api_key="test", request=request)
    outbox = LoopOutbox(tmp_path / "outcomes.sqlite3")
    with pytest.raises(RuntimeError, match="outcome delivery failure"):
        _stage_and_deliver(
            [outcome], client=client, outbox=outbox, stage_only=False,
            observed_before=NOW, statuses={},
        )
    assert calls == [
        "/api/v1/knowledge/quant/outcomes", "/api/v1/knowledge/quant/outcome-sync-statuses",
    ]
    item = outbox.get(outcome.id)
    assert item is not None and item.status == "failed"
    assert item.last_error_code == "RuntimeError"
    assert "sync-status" in caplog.text and "ValueError" in caplog.text


def _accepted_outcome_inputs(tmp_path: Path) -> tuple[Path, OutcomeReporterConfig]:
    data_root = tmp_path / "data"
    for day, close in [(date(2026, 8, 31), 100.0), (date(2026, 9, 1), 104.0)]:
        frame = canonicalize_daily_bars(pl.DataFrame({
            "symbol": ["AAPL", "QQQ"], "trade_date": [day, day],
            "provider_ts_utc": [NOW, NOW], "open": [99.0, 99.0],
            "high": [105.0, 105.0], "low": [98.0, 98.0], "close": [close, close],
            "volume": [1_000_000.0, 1_000_000.0], "trade_count": [10_000, 10_000],
            "vwap": [close, close], "source": ["massive.grouped_daily"] * 2,
            "feed": ["sip"] * 2, "adjustment": ["split_adjusted"] * 2,
        }))
        persist_snapshot(
            frame, root=data_root, source="massive.grouped_daily", schema_version="bars_daily.v1",
            checks=audit_daily_bars(frame, provenance="test.synthetic", expected_date=day),
        )
    return data_root, OutcomeReporterConfig(
        benchmark_symbol="QQQ", transaction_cost_bps_round_trip=10,
        slippage_bps_round_trip=5, cost_model_version="test-cost-v1",
        approved_by="test-owner", approved_at_utc=NOW,
    )


def _outcome_assignment(*, event: bool, revision_id: str = "test-revision") -> dict[str, Any]:
    assignment: dict[str, Any] = {
        "decision_event_id": "test-event" if event else "test-strategy-event",
        "source_run_id": "test-run", "market_scope": "US-equity", "instrument": "AAPL",
        "decision_trading_date": "2026-08-31", "observed_verdict": "accept",
        "outstanding_horizons": ["1d", "5d"],
    }
    if not event:
        assignment.update(
            strategy_revision_id=revision_id, strategy_lineage_id="test-lineage",
            target_verdict="accept", evaluation_role="forward",
        )
    return assignment


@pytest.mark.parametrize("failure", ["404", "timeout", "incomplete"])
@pytest.mark.parametrize("failed_checkpoint", [1, 2])
def test_diagnostic_failure_does_not_block_strategy_outcomes_and_can_retry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, failure: str, failed_checkpoint: int,
) -> None:
    data_root, config = _accepted_outcome_inputs(tmp_path)
    status_calls = 0
    outcome_ids: list[str] = []
    fail_enabled = True

    def request(method: str, path: str, payload: Any) -> Any:
        nonlocal status_calls
        if method == "GET":
            return [_outcome_assignment(event="event-outcome-assignments" in path)]
        if path.endswith("outcome-sync-statuses"):
            status_calls += 1
            if fail_enabled and status_calls == failed_checkpoint:
                if failure == "404":
                    response = httpx.Response(
                        404, request=httpx.Request("POST", "https://loop.invalid" + path)
                    )
                    response.raise_for_status()
                if failure == "timeout":
                    raise httpx.ReadTimeout("synthetic status timeout")
                return {"saved": 0}
            return {"saved": len(payload["statuses"])}
        outcome_ids.append(payload["id"])
        return {"id": payload["id"]}

    client = LoopClient(base_url="https://loop.invalid", api_key="test", request=request)
    outbox = LoopOutbox(tmp_path / "outcomes.sqlite3")
    args: dict[str, Any] = dict(
        client=client, outbox=outbox, data_root=data_root, as_of_date=date(2026, 9, 1),
        observed_before=datetime.now(UTC) + timedelta(seconds=1), config=config,
    )
    summary = sync_due_outcomes(**args)
    assert (summary.staged, summary.delivered) == (2, 2)
    assert len(outcome_ids) == 2
    for outcome_id in outcome_ids:
        item = outbox.get(outcome_id)
        assert item is not None and item.status == "delivered"
    assert "sync-status" in caplog.text
    assert {"404": "HTTPStatusError", "timeout": "ReadTimeout", "incomplete": "RuntimeError"}[
        failure
    ] in caplog.text

    fail_enabled = False
    previous_status_calls = status_calls
    replay = sync_due_outcomes(**args)
    assert (replay.staged, replay.delivered) == (2, 2)
    assert len(outcome_ids) == 2  # Retry diagnostics without duplicating Outcome delivery.
    assert status_calls > previous_status_calls


@pytest.mark.parametrize("revision_length", [80, 128])
@pytest.mark.parametrize("stage_only", [True, False])
def test_legal_long_revision_is_preserved_through_outcome_staging_and_statuses(
    tmp_path: Path, revision_length: int, stage_only: bool,
) -> None:
    data_root, config = _accepted_outcome_inputs(tmp_path)
    revision_id = "r" * revision_length
    posted: list[dict[str, Any]] = []
    status_rows: list[dict[str, Any]] = []

    def request(method: str, path: str, payload: Any) -> Any:
        if method == "GET":
            if "event-outcome-assignments" in path:
                return []
            return [_outcome_assignment(event=False, revision_id=revision_id)]
        assert not stage_only
        if path.endswith("outcome-sync-statuses"):
            status_rows.extend(payload["statuses"])
            return {"saved": len(payload["statuses"])}
        posted.append(payload)
        return {"id": payload["id"]}

    outbox = LoopOutbox(tmp_path / "outcomes.sqlite3")
    summary = sync_due_outcomes(
        client=LoopClient(base_url="https://loop.invalid", api_key="test", request=request),
        outbox=outbox, data_root=data_root, as_of_date=date(2026, 9, 1),
        observed_before=datetime.now(UTC) + timedelta(seconds=1), config=config,
        stage_only=stage_only,
    )
    assert (summary.staged, summary.delivered) == (1, 0 if stage_only else 1)
    assert summary.pending[0].strategy_revision_id == revision_id
    if stage_only:
        item = outbox.pending()[0]
        assert item.status == "pending"
        assert not posted and not status_rows
    else:
        fetched = outbox.get(posted[0]["id"])
        assert fetched is not None and fetched.status == "delivered"
        item = fetched
        assert {row["state"] for row in status_rows} == {"OBSERVED", "NOT_MATURED"}
        assert all(row["strategy_revision_id"] == revision_id for row in status_rows)
    assert item.payload["evidence"]["strategy_revision_id"] == revision_id
