from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from operations.loop_integration import execution_summary, outcome_reporter
from operations.loop_integration.contracts import LoopOutcomeEnvelope

TRADE_DATE = date(2026, 9, 1)
AS_OF = datetime(2026, 9, 1, 21, tzinfo=UTC)


def pinned_json(path: Path, payload: dict[str, Any]) -> str:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan_payload(active_policy_hash: str = "a" * 64) -> dict[str, Any]:
    strategy = {
        "strategy_id": "modern-h15",
        "strategy_version": "modern-h15-current-signal.v4",
        "active_policy_hash": active_policy_hash,
        "parameters": {
            "minimum_market_cap": 1_000_000_000.0, "minimum_premarket_rvol": 1.5,
            "minimum_h15_volume": 100_000, "minimum_gap_return": 0.04,
            "target_r": 3.0, "max_all_in_stop_pct": 0.02, "relative_spread": 0.001,
            "maximum_entry_relative_spread": 0.0025, "market_impact_pct": 0.0002,
            "stop_slippage_reserve_pct": 0.005, "signal_cutoff_minutes": 330,
            "liquidation_minutes": 380,
        },
        "risk_policy": {
            "symbol_risk_fraction": 0.005,
            "maximum_all_in_stop_pct": 0.02,
            "new_entry_cutoff_et": "15:00",
            "flatten_et": "15:50",
            "attempt_weights": [0.6, 0.4],
        },
    }
    strategy_hash = hashlib.sha256(
        json.dumps(strategy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": "loop_effective_modern_plan.v1",
        "trade_date": TRADE_DATE.isoformat(),
        "strategy": strategy,
        "strategy_sha256": strategy_hash,
        "effective_at_utc": "2026-09-01T13:20:00+00:00",
        "available_at_utc": "2026-09-01T13:20:00+00:00",
        "selection_cutoff_utc": "2026-09-01T13:25:00+00:00",
        "candidate_pool_available_at_utc": "2026-09-01T13:25:00+00:00",
        "source_snapshot_ids": ["frozen-effective-plan", "full-morning-pool"],
        "candidate_pool_complete": True,
        "candidate_pool_source": "complete_frozen_morning_pool",
        "candidates": [
            {"symbol": "MORNING", "verdict": "accept", "reason": "frozen decision"},
            {"symbol": "WATCH", "verdict": "watch", "reason": "frozen decision"},
        ],
    }


def test_accept_without_broker_evidence_never_becomes_execution_pnl(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    plan_hash = pinned_json(plan_path, plan_payload())
    result = execution_summary.build_factual_execution_summary(
        plan_path=plan_path, plan_sha256=plan_hash, trade_date=TRADE_DATE, as_of=AS_OF
    )
    assert result["status"] == "unavailable"
    assert result["realized_gross_pnl"] is None
    assert result["realized_net_pnl"] is None
    assert result["fees"] is None
    assert result["strategy_sha256"] == plan_payload()["strategy_sha256"]
    assert result["trade_date"] == "2026-09-01"
    assert result["orders_authorized"] is False


def fills_payload(plan_hash: str, fills: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "loop_broker_fills.v1",
        "evidence_kind": "broker_confirmed_fills",
        "quantity_semantics": "incremental_execution",
        "trade_date": "2026-09-01",
        "strategy_sha256": plan_payload()["strategy_sha256"],
        "plan_sha256": plan_hash,
        "broker": "test-broker",
        "account_id": "test-account",
        "environment": "paper",
        "currency": "USD",
        "source": "unit-test frozen broker ledger",
        "validated_by": "unit-test reconciliation",
        "coverage_start_utc": "2026-09-01T13:30:00+00:00",
        "coverage_end_utc": "2026-09-01T20:00:00+00:00",
        "opening_positions_flat": True,
        "reconciled_complete": True,
        "costs_complete": True,
        "fills": fills,
    }


def test_confirmed_empty_ledger_is_no_trade_but_order_intent_is_not_a_fill(tmp_path: Path) -> None:
    plan_path, fill_path = tmp_path / "plan.json", tmp_path / "fills.json"
    plan_hash = pinned_json(plan_path, plan_payload())
    evidence = fills_payload(plan_hash, [])
    fill_hash = pinned_json(fill_path, evidence)
    result = execution_summary.build_factual_execution_summary(
        plan_path=plan_path, plan_sha256=plan_hash, trade_date=TRADE_DATE, as_of=AS_OF,
        fills_path=fill_path, fills_sha256=fill_hash,
    )
    assert result["status"] == "no_trade"
    assert result["fill_count"] == 0
    assert result["realized_gross_pnl"] is None
    assert result["realized_net_pnl"] is None
    evidence["evidence_kind"] = "local_order_intent"
    fill_hash = pinned_json(fill_path, evidence)
    with pytest.raises(ValueError, match="broker_confirmed_fills"):
        execution_summary.build_factual_execution_summary(
            plan_path=plan_path, plan_sha256=plan_hash, trade_date=TRADE_DATE, as_of=AS_OF,
            fills_path=fill_path, fills_sha256=fill_hash,
        )


def fill(fill_id: str, side: str, quantity: str, price: str, hour: int) -> dict[str, Any]:
    return {
        "fill_id": fill_id,
        "broker_order_id": "broker-order-buy" if side == "buy" else "broker-order-sell",
        "symbol": "MORNING",
        "side": side,
        "quantity": quantity,
        "price": price,
        "filled_at_utc": f"2026-09-01T{hour}:00:00+00:00",
        "fees": "1",
        "fee_source": f"broker-fee:{fill_id}",
        "source": f"broker-execution:{fill_id}",
        "broker_confirmed": True,
    }


@pytest.mark.parametrize("unknown_fee", [False, True])
def test_partial_fills_use_matched_quantity_and_never_default_missing_costs(
    tmp_path: Path, unknown_fee: bool
) -> None:
    plan_path, fill_path = tmp_path / "plan.json", tmp_path / "fills.json"
    plan_hash = pinned_json(plan_path, plan_payload())
    rows = [
        fill("buy-1", "buy", "4", "100", 14),
        fill("buy-2", "buy", "6", "110", 15),
        fill("sell-1", "sell", "5", "120", 16),
    ]
    if unknown_fee:
        rows[0]["fees"] = None
        rows[0]["fee_source"] = None
    fill_hash = pinned_json(fill_path, fills_payload(plan_hash, rows))
    result = execution_summary.build_factual_execution_summary(
        plan_path=plan_path, plan_sha256=plan_hash, trade_date=TRADE_DATE, as_of=AS_OF,
        fills_path=fill_path, fills_sha256=fill_hash,
    )
    assert result["status"] == "partial_fills"
    assert result["fill_count"] == 3
    assert result["matched_quantity"] == 5
    assert result["open_quantity"] == 5
    # FIFO: 4 * (120 - 100) + 1 * (120 - 110) = 90 USD.
    assert result["realized_gross_pnl"] == 90
    assert result["unrealized_pnl"] is None
    if unknown_fee:
        assert result["fees"] is None
        assert result["realized_net_pnl"] is None
        assert result["cost_status"] == "unavailable"
    else:
        assert result["fees"] == 3
        assert result["realized_fees"] == pytest.approx(2 + 1 / 6)
        assert result["realized_net_pnl"] == pytest.approx(87 + 5 / 6)
    assert result["broker_evidence"]["fills"] == rows


def test_factual_summary_cli_is_local_read_only(tmp_path: Path) -> None:
    plan_path, fill_path = tmp_path / "plan.json", tmp_path / "fills.json"
    plan_hash = pinned_json(plan_path, plan_payload())
    fill_hash = pinned_json(fill_path, fills_payload(plan_hash, []))
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    result = subprocess.run(
        [sys.executable, "-B", "-m", "scripts.summarize_loop_execution",
         "--plan", str(plan_path), "--plan-sha256", plan_hash,
         "--fills", str(fill_path), "--fills-sha256", fill_hash,
         "--trade-date", "2026-09-01", "--as-of", AS_OF.isoformat()],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "no_trade"
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize("case", ["config_hash", "missing_risk", "future_provenance", "file_hash"])
def test_effective_plan_rejects_missing_or_mismatched_evidence(tmp_path: Path, case: str) -> None:
    payload = plan_payload()
    if case == "config_hash":
        payload["strategy"]["parameters"]["target_r"] = 4
    elif case == "missing_risk":
        del payload["strategy"]["risk_policy"]["symbol_risk_fraction"]
    elif case == "future_provenance":
        payload["available_at_utc"] = "2026-09-01T21:01:00Z"
    path = tmp_path / "plan.json"
    digest = pinned_json(path, payload)
    with pytest.raises(ValueError):
        execution_summary.build_factual_execution_summary(
            plan_path=path, plan_sha256="0" * 64 if case == "file_hash" else digest,
            trade_date=TRADE_DATE, as_of=AS_OF,
        )


@pytest.mark.parametrize("case", [
    "strategy", "plan", "date", "duplicate", "oversell", "not_confirmed",
    "zero_fee_no_source", "outside_pool", "same_order_different_side",
])
def test_factual_execution_rejects_broken_broker_lineage(tmp_path: Path, case: str) -> None:
    plan_path, fill_path = tmp_path / "plan.json", tmp_path / "fills.json"
    plan_hash = pinned_json(plan_path, plan_payload())
    rows = [fill("buy-1", "buy", "4", "100", 14), fill("sell-1", "sell", "4", "120", 15)]
    evidence = fills_payload(plan_hash, rows)
    if case in {"strategy", "plan"}:
        evidence[f"{case}_sha256"] = "b" * 64
    elif case == "date":
        evidence["trade_date"] = "2026-09-02"
    elif case == "duplicate":
        rows[1]["fill_id"] = rows[0]["fill_id"]
    elif case == "oversell":
        rows[1]["quantity"] = "5"
    elif case == "not_confirmed":
        rows[0]["broker_confirmed"] = False
    elif case == "zero_fee_no_source":
        rows[0]["fees"], rows[0]["fee_source"] = "0", None
    elif case == "outside_pool":
        rows[0]["symbol"] = "WINNER"
    else:
        rows[1]["broker_order_id"] = rows[0]["broker_order_id"]
    fill_hash = pinned_json(fill_path, evidence)
    with pytest.raises(ValueError):
        execution_summary.build_factual_execution_summary(
            plan_path=plan_path, plan_sha256=plan_hash, trade_date=TRADE_DATE, as_of=AS_OF,
            fills_path=fill_path, fills_sha256=fill_hash,
        )


@pytest.mark.parametrize("incomplete", ["reconciled_complete", "costs_complete"])
def test_incomplete_evidence_cannot_claim_net_pnl(tmp_path: Path, incomplete: str) -> None:
    plan_path, fill_path = tmp_path / "plan.json", tmp_path / "fills.json"
    plan_hash = pinned_json(plan_path, plan_payload())
    rows = [fill("buy-1", "buy", "4", "100", 14), fill("sell-1", "sell", "4", "120", 15)]
    evidence = fills_payload(plan_hash, rows)
    evidence[incomplete] = False
    fill_hash = pinned_json(fill_path, evidence)
    result = execution_summary.build_factual_execution_summary(
        plan_path=plan_path, plan_sha256=plan_hash, trade_date=TRADE_DATE, as_of=AS_OF,
        fills_path=fill_path, fills_sha256=fill_hash,
    )
    assert result["realized_net_pnl"] is None
    assert result["fees"] is None
    assert result["realized_gross_pnl"] == (80 if incomplete == "costs_complete" else None)


def test_cumulative_broker_snapshots_must_not_be_summed_as_incremental_fills(
    tmp_path: Path,
) -> None:
    plan_path, fill_path = tmp_path / "plan.json", tmp_path / "fills.json"
    plan_hash = pinned_json(plan_path, plan_payload())
    evidence = fills_payload(plan_hash, [
        fill("fill:broker-1:4", "buy", "4", "100", 14),
        fill("fill:broker-1:10", "buy", "10", "106", 15),
    ])
    evidence["quantity_semantics"] = "broker_order_cumulative"
    fill_hash = pinned_json(fill_path, evidence)
    with pytest.raises(ValueError, match="incremental_execution"):
        execution_summary.build_factual_execution_summary(
            plan_path=plan_path, plan_sha256=plan_hash, trade_date=TRADE_DATE, as_of=AS_OF,
            fills_path=fill_path, fills_sha256=fill_hash,
        )


def test_outcome_links_factual_performance_only_by_date_strategy_hash_and_instrument(
    tmp_path: Path,
) -> None:
    plan_path, fill_path = tmp_path / "plan.json", tmp_path / "fills.json"
    plan_hash = pinned_json(plan_path, plan_payload())
    rows = [fill("buy-1", "buy", "4", "100", 14), fill("sell-1", "sell", "4", "120", 15)]
    fill_hash = pinned_json(fill_path, fills_payload(plan_hash, rows))
    outcome = LoopOutcomeEnvelope(
        id="research-outcome", decision_event_id="decision-1", source_run_id="run-1",
        market_scope="US-equity", instrument="MORNING", horizon="1d", observed_at=AS_OF,
        strategy_return=0.7, evidence={
            "strategy_revision_id": "revision-1", "evaluation_role": "forward",
            "point_in_time_guard_passed": True, "decision_trading_date": "2026-09-01",
            "strategy_sha256": plan_payload()["strategy_sha256"],
        }, metadata={},
    )
    linked = outcome_reporter.attach_factual_execution(
        outcome, plan_path=plan_path, plan_sha256=plan_hash,
        fills_path=fill_path, fills_sha256=fill_hash,
    )
    assert linked.id != outcome.id
    assert linked.strategy_return == 0.7  # Never replace market research with trade returns.
    actual = linked.evidence["factual_execution"]
    assert actual["realized_gross_pnl"] == 80
    assert actual["realized_net_pnl"] == 78
    assert actual["fill_ids"] == ["buy-1", "sell-1"]
    assert actual["trade_date"] == "2026-09-01"
    assert actual["strategy_sha256"] == plan_payload()["strategy_sha256"]
    assert "factual_execution" not in outcome.evidence
    for field, value in [("strategy_sha256", "b" * 64), ("decision_trading_date", "2026-09-02")]:
        wrong = LoopOutcomeEnvelope.model_validate({
            **outcome.model_dump(mode="json"), "evidence": {**outcome.evidence, field: value},
        })
        with pytest.raises(ValueError, match="mismatch"):
            outcome_reporter.attach_factual_execution(
                wrong, plan_path=plan_path, plan_sha256=plan_hash,
                fills_path=fill_path, fills_sha256=fill_hash,
            )


def native_plan_payload() -> dict[str, Any]:
    # Original final-plan fields remain native; review context is supplied from
    # frozen provider evidence, not inferred from the final selected candidates.
    context = plan_payload()
    context["effective_at_utc"] = "2026-09-01T13:35:00Z"
    context["available_at_utc"] = "2026-09-01T13:35:00Z"
    return {
        "schema_version": "modern_h15_paper_plan.v1", "trade_date": "2026-09-01",
        "strategy_version": "modern-h15.v1", "paper_only": True,
        "entry_after_et": "09:56", "new_entry_cutoff_et": "15:00",
        "cancel_unfilled_et": "15:45", "flatten_et": "15:50",
        "maximum_all_in_stop_pct": 0.02, "target_r": 3,
        "symbol_risk_fraction": 0.005, "attempt_weights": [0.6, 0.4],
        "maximum_entry_relative_spread": 0.0025,
        "modern_strategy_manifest": {
            "schema_version": "modern_strategy_manifest.v1",
            "strategy_version": context["strategy"]["strategy_version"],
            "effective_config": context["strategy"]["parameters"],
            "config_sha256": execution_summary.canonical_sha256(context["strategy"]["parameters"]),
            "first_entry_bar_minutes": 1, "reentry_bar_minutes": 5,
        },
        "candidates": [{"symbol": "MORNING"}], "loop_review": context,
    }


@pytest.mark.parametrize("case", [
    "symbol_risk", "spread", "parameters", "manifest_version", "manifest_hash", "missing_manifest",
])
def test_native_adapter_requires_matching_actual_manifest_and_risk(
    tmp_path: Path, case: str,
) -> None:
    native = native_plan_payload()
    context = json.loads(json.dumps(native.pop("loop_review")))
    # Also support producers that use the effective version in authorization.
    native["strategy_version"] = context["strategy"]["strategy_version"]
    if case == "symbol_risk":
        context["strategy"]["risk_policy"]["symbol_risk_fraction"] = 0.004
    elif case == "spread":
        native["maximum_entry_relative_spread"] = 0.001
    elif case == "parameters":
        context["strategy"]["parameters"]["minimum_gap_return"] = 0.05
    elif case == "manifest_version":
        native["modern_strategy_manifest"]["strategy_version"] = "modern-h15-other"
    elif case == "manifest_hash":
        native["modern_strategy_manifest"]["config_sha256"] = "b" * 64
    else:
        del native["modern_strategy_manifest"]
    context["strategy_sha256"] = execution_summary.canonical_sha256(context["strategy"])
    plan_path, context_path = tmp_path / "native.json", tmp_path / "context.json"
    plan_hash = pinned_json(plan_path, native)
    context["native_plan_sha256"] = plan_hash
    context_hash = pinned_json(context_path, context)
    with pytest.raises(ValueError):
        execution_summary.load_effective_plan(
            plan_path, expected_sha256=plan_hash, trade_date=TRADE_DATE, as_of=AS_OF,
            review_context_path=context_path, review_context_sha256=context_hash,
        )


def test_native_0935_plan_after_0925_pool_is_valid_but_cannot_explain_earlier_fills(
    tmp_path: Path,
) -> None:
    plan_path, fill_path = tmp_path / "native.json", tmp_path / "fills.json"
    native = native_plan_payload()
    context = native.pop("loop_review")
    plan_hash = pinned_json(plan_path, native)
    context["native_plan_sha256"] = plan_hash
    context_path = tmp_path / "context.json"
    context_hash = pinned_json(context_path, context)
    rows = [fill("buy-1", "buy", "4", "100", 14)]
    fill_hash = pinned_json(fill_path, fills_payload(plan_hash, rows))
    summary = execution_summary.build_factual_execution_summary(
        plan_path=plan_path, plan_sha256=plan_hash, trade_date=TRADE_DATE, as_of=AS_OF,
        fills_path=fill_path, fills_sha256=fill_hash,
        review_context_path=context_path, review_context_sha256=context_hash,
    )
    assert summary["status"] == "partial_fills"
    rows[0]["filled_at_utc"] = "2026-09-01T13:34:00Z"
    fill_hash = pinned_json(fill_path, fills_payload(plan_hash, rows))
    with pytest.raises(ValueError, match="precedes effective plan"):
        execution_summary.build_factual_execution_summary(
            plan_path=plan_path, plan_sha256=plan_hash, trade_date=TRADE_DATE, as_of=AS_OF,
            fills_path=fill_path, fills_sha256=fill_hash,
            review_context_path=context_path, review_context_sha256=context_hash,
        )
