"""Delayed close-to-close market counterfactuals, never intraday execution PnL.

The v2 wire fields strategy_return/excess_return/costs remain research aliases
for compatibility. A target accept is a hypothetical long, not a broker fill.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DatasetSnapshot
from data_plane.storage import sha256_file

from .client import LoopClient
from .contracts import (
    EVENT_OUTCOME_EXCESS_FORMULA,
    OUTCOME_EXCESS_FORMULA,
    OUTCOME_HORIZON_SESSIONS,
    LoopEventOutcomeAssignment,
    LoopOutcomeAssignment,
    LoopOutcomeEnvelope,
    OutcomeReporterConfig,
)
from .execution_summary import (
    build_factual_execution_summary,
    load_execution_index,
    unavailable_execution,
)
from .outbox import LoopOutbox
from .review_builder import envelope_sha256


@dataclass(frozen=True)
class PendingOutcome:
    decision_event_id: str
    strategy_revision_id: str
    horizon: str
    reason: str


@dataclass(frozen=True)
class OutcomeSyncSummary:
    assignments: int
    event_assignments: int
    strategy_assignments: int
    due: int
    staged: int
    delivered: int
    pending: tuple[PendingOutcome, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "completed" if self.due == self.delivered and all(
                item.reason == "horizon_not_mature" for item in self.pending
            ) else "pending",
            "assignments": self.assignments,
            "event_assignments": self.event_assignments,
            "strategy_assignments": self.strategy_assignments,
            "due": self.due,
            "staged": self.staged,
            "delivered": self.delivered,
            "pending": [item.__dict__ for item in self.pending],
        }


@dataclass(frozen=True)
class _DailySnapshot:
    snapshot: DatasetSnapshot
    path: Path
    frame: pl.DataFrame


def _load_daily_index(
    data_root: Path,
    *,
    config: OutcomeReporterConfig,
    observed_before: datetime,
) -> dict[date, _DailySnapshot]:
    result: dict[date, _DailySnapshot] = {}
    for path in (data_root / "accepted").glob(f"{config.price_source}-*/data.parquet"):
        snapshot = DatasetSnapshot.model_validate_json(
            (path.parent / "manifest.json").read_text(encoding="utf-8")
        ).assert_usable()
        if snapshot.source != config.price_source or snapshot.asof_utc > observed_before:
            continue
        if snapshot.dataset_id != path.parent.name:
            raise ValueError("accepted daily snapshot directory does not match dataset id")
        if sha256_file(path) != snapshot.content_sha256:
            raise ValueError(f"accepted daily snapshot hash mismatch: {snapshot.dataset_id}")
        frame = pl.read_parquet(path)
        required = {"symbol", "trade_date", "close", "low", "adjustment"}
        if missing := required - set(frame.columns):
            raise ValueError(f"daily snapshot {snapshot.dataset_id} misses {sorted(missing)}")
        dates = frame.get_column("trade_date").cast(pl.Date).unique().to_list()
        if len(dates) != 1 or not isinstance(dates[0], date):
            raise ValueError(f"daily snapshot {snapshot.dataset_id} must contain one trading date")
        adjustments = {str(value) for value in frame.get_column("adjustment").unique().to_list()}
        if adjustments != {config.adjustment}:
            raise ValueError(f"daily snapshot {snapshot.dataset_id} adjustment mismatch")
        candidate = _DailySnapshot(snapshot=snapshot, path=path, frame=frame)
        previous = result.get(dates[0])
        if previous is None or previous.snapshot.asof_utc < snapshot.asof_utc:
            result[dates[0]] = candidate
    return result


def _session_rows(decision_date: date, as_of_date: date) -> list[dict[str, Any]]:
    if as_of_date <= decision_date:
        return []
    schedule = build_xnys_schedule(decision_date, as_of_date)
    return [
        row
        for row in schedule.iter_rows(named=True)
        if isinstance(row["trade_date"], date) and row["trade_date"] > decision_date
    ]


def _price_row(snapshot: _DailySnapshot, symbol: str) -> dict[str, Any] | None:
    rows = snapshot.frame.filter(pl.col("symbol") == symbol).select(
        "symbol", "trade_date", "close", "low"
    )
    if rows.height != 1:
        return None
    row = rows.row(0, named=True)
    close = float(row["close"])
    low = float(row["low"])
    if not math.isfinite(close) or not math.isfinite(low) or close <= 0 or low <= 0:
        return None
    return dict(row)


def _outcome_id(
    assignment: LoopOutcomeAssignment,
    *,
    horizon: Literal["1d", "5d", "20d"],
    snapshot_ids: list[str],
    config: OutcomeReporterConfig,
) -> str:
    identity = {
        "decision_event_id": assignment.decision_event_id,
        "strategy_revision_id": assignment.strategy_revision_id,
        "strategy_sha256": assignment.strategy_sha256,
        "horizon": horizon,
        "snapshot_ids": snapshot_ids,
        "cost_model_version": config.cost_model_version,
        "return_semantics": "market_counterfactual.close_to_close_split_adjusted.v3",
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"quant_outcome_v3_{digest[:40]}"


def _event_outcome_id(
    assignment: LoopEventOutcomeAssignment,
    *,
    horizon: Literal["1d", "5d", "20d"],
    snapshot_ids: list[str],
    config: OutcomeReporterConfig,
) -> str:
    identity = {
        "decision_event_id": assignment.decision_event_id,
        "horizon": horizon,
        "snapshot_ids": snapshot_ids,
        "cost_model_version": config.cost_model_version,
        "return_semantics": "close_to_close_split_adjusted.event.v1",
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"quant_event_outcome_v1_{digest[:36]}"


def build_due_event_outcome(
    assignment: LoopEventOutcomeAssignment,
    *,
    horizon: Literal["1d", "5d", "20d"],
    as_of_date: date,
    daily_index: dict[date, _DailySnapshot],
    config: OutcomeReporterConfig,
) -> tuple[LoopOutcomeEnvelope | None, str]:
    sessions = _session_rows(assignment.decision_trading_date, as_of_date)
    session_count = OUTCOME_HORIZON_SESSIONS[horizon]
    if len(sessions) < session_count:
        return None, "horizon_not_mature"
    selected_sessions = sessions[:session_count]
    horizon_row = selected_sessions[-1]
    horizon_date = horizon_row["trade_date"]
    horizon_close = horizon_row["market_close_utc"]
    if not isinstance(horizon_date, date) or not isinstance(horizon_close, datetime):
        raise ValueError("XNYS calendar returned invalid horizon fields")
    required_dates = [assignment.decision_trading_date] + [
        row["trade_date"] for row in selected_sessions
    ]
    missing_dates = [value for value in required_dates if value not in daily_index]
    if missing_dates:
        return None, f"daily_snapshot_missing:{missing_dates[0].isoformat()}"
    snapshots = [daily_index[value] for value in required_dates]
    instrument_rows = [_price_row(snapshot, assignment.instrument) for snapshot in snapshots]
    if any(row is None for row in instrument_rows):
        return None, f"instrument_bar_missing_or_halted:{assignment.instrument}"
    benchmark_rows = [_price_row(snapshot, config.benchmark_symbol) for snapshot in snapshots]
    if any(row is None for row in benchmark_rows):
        return None, f"benchmark_bar_missing:{config.benchmark_symbol}"
    instrument = [row for row in instrument_rows if row is not None]
    benchmark = [row for row in benchmark_rows if row is not None]
    instrument_start = float(instrument[0]["close"])
    benchmark_start = float(benchmark[0]["close"])
    instrument_return = float(instrument[-1]["close"]) / instrument_start - 1.0
    benchmark_return = float(benchmark[-1]["close"]) / benchmark_start - 1.0
    counterfactual_drawdown = min(
        0.0,
        *(float(row["low"]) / instrument_start - 1.0 for row in instrument[1:]),
    )
    counterfactual_cost_multiplier = (
        2.0 if assignment.observed_verdict in {"reject", "block"} else 1.0
    )
    transaction_cost = (
        config.transaction_cost_bps_round_trip * counterfactual_cost_multiplier / 10_000
    )
    slippage = config.slippage_bps_round_trip * counterfactual_cost_multiplier / 10_000
    net_excess_return = instrument_return - benchmark_return - transaction_cost - slippage
    if assignment.observed_verdict == "accept":
        direction_correct = net_excess_return > 0
        correctness_rule = "accept iff instrument net excess return is positive"
    elif assignment.observed_verdict in {"reject", "block"}:
        direction_correct = net_excess_return <= 0
        correctness_rule = "reject/block iff avoided instrument net excess return is non-positive"
    else:
        neutral_band = config.watch_neutral_band_bps / 10_000
        direction_correct = abs(net_excess_return) <= neutral_band
        correctness_rule = "watch iff absolute instrument net excess return is within neutral band"
    snapshot_ids = [item.snapshot.dataset_id for item in snapshots]
    observed_at = max(
        horizon_close.astimezone(UTC),
        *(item.snapshot.asof_utc for item in snapshots),
    )
    evidence = {
        "schema_version": "quant-event-outcome-evidence-v1",
        "evaluation_role": "raw_event",
        "point_in_time_guard_passed": True,
        "decision_trading_date": assignment.decision_trading_date.isoformat(),
        "horizon_end_trading_date": horizon_date.isoformat(),
        "horizon_end_market_close_utc": horizon_close.astimezone(UTC).isoformat(),
        "trading_session_dates": [row["trade_date"].isoformat() for row in selected_sessions],
        "trading_calendar": {
            "name": "XNYS",
            "source": str(horizon_row["source"]),
            "version": str(horizon_row["source_version"]),
        },
        "benchmark_id": config.benchmark_symbol,
        "price_snapshot_ids": snapshot_ids,
        "return_semantics": {
            "unit": "decimal_fraction",
            "method": "close_to_close_split_adjusted",
            "return_basis": "observed_instrument",
            "excess_return_formula": EVENT_OUTCOME_EXCESS_FORMULA,
            "realized_policy_return_basis": "not_applicable_raw_event",
            "counterfactual_cost_multiplier": counterfactual_cost_multiplier,
        },
        "observed_verdict": assignment.observed_verdict,
        "direction_correctness_rule": correctness_rule,
        "watch_neutral_band_bps": config.watch_neutral_band_bps,
        "cost_model_version": config.cost_model_version,
        "cost_model_approved_by": config.approved_by,
        "cost_model_approved_at_utc": config.approved_at_utc.isoformat(),
        "synthetic": False,
    }
    return (
        LoopOutcomeEnvelope(
            schema_version="ai_quant.loop_event_outcome.v1",
            id=_event_outcome_id(
                assignment,
                horizon=horizon,
                snapshot_ids=snapshot_ids,
                config=config,
            ),
            decision_event_id=assignment.decision_event_id,
            source_run_id=assignment.source_run_id,
            market_scope=assignment.market_scope,
            instrument=assignment.instrument,
            horizon=horizon,
            outcome_kind="event_observation",
            observed_at=observed_at,
            instrument_return=instrument_return,
            realized_policy_return=None,
            counterfactual_instrument_return=instrument_return,
            counterfactual_net_excess_return=net_excess_return,
            benchmark_return=benchmark_return,
            excess_return=net_excess_return,
            max_drawdown=counterfactual_drawdown,
            transaction_cost=transaction_cost,
            slippage=slippage,
            direction_correct=direction_correct,
            evidence=evidence,
            metadata={
                "source_system": "ai-quant-trading-system",
                "synthetic": False,
                "production_eligible": False,
                "allow_order_execution": False,
            },
        ),
        "ready",
    )


def build_due_outcome(
    assignment: LoopOutcomeAssignment,
    *,
    horizon: Literal["1d", "5d", "20d"],
    as_of_date: date,
    daily_index: dict[date, _DailySnapshot],
    config: OutcomeReporterConfig,
) -> tuple[LoopOutcomeEnvelope | None, str]:
    sessions = _session_rows(assignment.decision_trading_date, as_of_date)
    session_count = OUTCOME_HORIZON_SESSIONS[horizon]
    if len(sessions) < session_count:
        return None, "horizon_not_mature"
    selected_sessions = sessions[:session_count]
    horizon_row = selected_sessions[-1]
    horizon_date = horizon_row["trade_date"]
    horizon_close = horizon_row["market_close_utc"]
    if not isinstance(horizon_date, date) or not isinstance(horizon_close, datetime):
        raise ValueError("XNYS calendar returned invalid horizon fields")
    required_dates = [assignment.decision_trading_date] + [
        row["trade_date"] for row in selected_sessions
    ]
    missing_dates = [value for value in required_dates if value not in daily_index]
    if missing_dates:
        return None, f"daily_snapshot_missing:{missing_dates[0].isoformat()}"
    snapshots = [daily_index[value] for value in required_dates]
    instrument_rows = [_price_row(snapshot, assignment.instrument) for snapshot in snapshots]
    if any(row is None for row in instrument_rows):
        return None, f"instrument_bar_missing_or_halted:{assignment.instrument}"
    benchmark_rows = [_price_row(snapshot, config.benchmark_symbol) for snapshot in snapshots]
    if any(row is None for row in benchmark_rows):
        return None, f"benchmark_bar_missing:{config.benchmark_symbol}"
    instrument = [row for row in instrument_rows if row is not None]
    benchmark = [row for row in benchmark_rows if row is not None]
    instrument_start = float(instrument[0]["close"])
    benchmark_start = float(benchmark[0]["close"])
    instrument_return = float(instrument[-1]["close"]) / instrument_start - 1.0
    benchmark_return = float(benchmark[-1]["close"]) / benchmark_start - 1.0
    counterfactual_drawdown = min(
        0.0,
        *(float(row["low"]) / instrument_start - 1.0 for row in instrument[1:]),
    )
    target_verdict = assignment.target_verdict
    enters_position = target_verdict == "accept"
    strategy_return = instrument_return if enters_position else 0.0
    counterfactual_selected_return = strategy_return
    counterfactual_cost_multiplier = 1.0
    transaction_cost = (
        config.transaction_cost_bps_round_trip / 10_000 if enters_position else 0.0
    )
    slippage = config.slippage_bps_round_trip / 10_000 if enters_position else 0.0
    excess_return = strategy_return - benchmark_return - transaction_cost - slippage
    counterfactual_net_alpha = (
        instrument_return
        - benchmark_return
        - config.transaction_cost_bps_round_trip / 10_000
        - config.slippage_bps_round_trip / 10_000
    )
    counterfactual_transaction_cost = config.transaction_cost_bps_round_trip / 10_000
    counterfactual_slippage = config.slippage_bps_round_trip / 10_000
    if target_verdict == "accept":
        direction_correct = counterfactual_net_alpha > 0
        correctness_rule = "accept iff instrument net excess return is positive"
    elif target_verdict in {"reject", "block"}:
        direction_correct = counterfactual_net_alpha <= 0
        correctness_rule = "reject/block iff avoided instrument net excess return is non-positive"
    else:
        neutral_band = config.watch_neutral_band_bps / 10_000
        direction_correct = abs(counterfactual_net_alpha) <= neutral_band
        correctness_rule = "watch iff absolute instrument net excess return is within neutral band"
    snapshot_ids = [item.snapshot.dataset_id for item in snapshots]
    observed_at = max(
        horizon_close.astimezone(UTC),
        *(item.snapshot.asof_utc for item in snapshots),
    )
    calendar_source = str(horizon_row["source"])
    calendar_version = str(horizon_row["source_version"])
    evidence = {
        "schema_version": "quant-outcome-evidence-v2",
        "strategy_revision_id": assignment.strategy_revision_id,
        "strategy_lineage_id": assignment.strategy_lineage_id,
        "strategy_sha256": assignment.strategy_sha256,
        "evaluation_role": assignment.evaluation_role,
        "point_in_time_guard_passed": True,
        "decision_trading_date": assignment.decision_trading_date.isoformat(),
        "horizon_end_trading_date": horizon_date.isoformat(),
        "horizon_end_market_close_utc": horizon_close.astimezone(UTC).isoformat(),
        "trading_session_dates": [row["trade_date"].isoformat() for row in selected_sessions],
        "trading_calendar": {
            "name": "XNYS",
            "source": calendar_source,
            "version": calendar_version,
        },
        "benchmark_id": config.benchmark_symbol,
        "price_snapshot_ids": snapshot_ids,
        "return_semantics": {
            "performance_kind": "market_counterfactual",
            "is_realized_trade_pnl": False,
            "holding_period": "decision_close_to_horizon_close_1_5_20_sessions",
            "cost_basis": "approved_research_assumptions_not_execution_fees",
            "legacy_strategy_return_alias": "counterfactual_selected_close_return",
            "unit": "decimal_fraction",
            "method": "close_to_close_split_adjusted",
            "strategy_return_basis": "gross_before_costs",
            "excess_return_formula": OUTCOME_EXCESS_FORMULA,
            "counterfactual_excess_return_formula": EVENT_OUTCOME_EXCESS_FORMULA,
            "realized_policy_return_basis": "unavailable_without_normalized_execution_return",
            "counterfactual_cost_multiplier": counterfactual_cost_multiplier,
        },
        "target_verdict": target_verdict,
        "observed_verdict": assignment.observed_verdict,
        "instrument_return": instrument_return,
        "counterfactual_selected_close_return": counterfactual_selected_return,
        "counterfactual_transaction_cost": counterfactual_transaction_cost,
        "counterfactual_slippage": counterfactual_slippage,
        "factual_execution": unavailable_execution(),
        "counterfactual_net_excess_return": counterfactual_net_alpha,
        "counterfactual_max_drawdown": counterfactual_drawdown,
        "direction_correctness_rule": correctness_rule,
        "watch_neutral_band_bps": config.watch_neutral_band_bps,
        "cost_model_version": config.cost_model_version,
        "cost_model_approved_by": config.approved_by,
        "cost_model_approved_at_utc": config.approved_at_utc.isoformat(),
        "synthetic": False,
    }
    if all(
        value not in (None, "")
        for value in (
            assignment.logging_policy_id,
            assignment.target_policy_id,
            assignment.logging_action_probability,
            assignment.target_probability_for_logged_action,
            assignment.reward_model_logged,
            assignment.reward_model_target,
        )
    ):
        evidence["policy_assignment"] = {
            "logging_policy_id": assignment.logging_policy_id,
            "logged_action": assignment.logged_action,
            "target_policy_id": assignment.target_policy_id,
            "logging_action_probability": assignment.logging_action_probability,
            "target_probability_for_logged_action": (
                assignment.target_probability_for_logged_action
            ),
            "reward_model_logged": assignment.reward_model_logged,
            "reward_model_target": assignment.reward_model_target,
        }
        evidence["policy_evaluation_status"] = "unavailable_without_realized_policy_return"
    outcome = LoopOutcomeEnvelope(
        schema_version="ai_quant.loop_outcome.v2",
        id=_outcome_id(
            assignment,
            horizon=horizon,
            snapshot_ids=snapshot_ids,
            config=config,
        ),
        decision_event_id=assignment.decision_event_id,
        source_run_id=assignment.source_run_id,
        market_scope=assignment.market_scope,
        instrument=assignment.instrument,
        horizon=horizon,
        observed_at=observed_at,
        instrument_return=instrument_return,
        strategy_return=strategy_return,
        realized_policy_return=None,
        counterfactual_instrument_return=instrument_return,
        counterfactual_net_excess_return=counterfactual_net_alpha,
        benchmark_return=benchmark_return,
        excess_return=excess_return,
        max_drawdown=counterfactual_drawdown if enters_position else 0.0,
        transaction_cost=transaction_cost,
        slippage=slippage,
        direction_correct=direction_correct,
        evidence=evidence,
        metadata={
            "source_system": "ai-quant-trading-system",
            "synthetic": False,
            "production_eligible": False,
            "allow_order_execution": False,
        },
    )
    return outcome, "ready"


def attach_factual_execution(
    outcome: LoopOutcomeEnvelope, *, plan_path: Path, plan_sha256: str,
    fills_path: Path, fills_sha256: str,
    review_context_path: Path | None = None, review_context_sha256: str | None = None,
) -> LoopOutcomeEnvelope:
    """Return a new artifact; never mutate a delivered Outcome or its research fields."""
    trade_date = date.fromisoformat(str(outcome.evidence.get("decision_trading_date", "")))
    summary = build_factual_execution_summary(
        plan_path=plan_path, plan_sha256=plan_sha256, fills_path=fills_path,
        fills_sha256=fills_sha256, trade_date=trade_date, as_of=outcome.observed_at,
        review_context_path=review_context_path, review_context_sha256=review_context_sha256,
    )
    if outcome.evidence.get("strategy_sha256") != summary["strategy_sha256"]:
        raise ValueError("Outcome and execution strategy hash mismatch or unavailable")
    performance = summary.get("instruments", {}).get(outcome.instrument)
    fills = [
        item for item in summary["broker_evidence"]["fills"]
        if item["symbol"] == outcome.instrument
    ]
    factual = {
        **(performance or unavailable_execution()),
        "trade_date": summary["trade_date"],
        "strategy_sha256": summary["strategy_sha256"],
        "plan_sha256": plan_sha256,
        "fill_evidence_sha256": fills_sha256,
        "review_context_sha256": review_context_sha256,
        "fill_ids": [item["fill_id"] for item in fills],
        "broker_evidence": summary["broker_evidence"],
        "performance_kind": "factual_broker_execution",
    }
    if performance is None:
        factual["reason"] = "instrument_execution_unavailable_or_incomplete"
    evidence = {**outcome.evidence, "factual_execution": factual}
    realized_policy_return = 0.0 if factual.get("status") == "no_trade" else None
    assignment = evidence.get("policy_assignment")
    if realized_policy_return is not None and isinstance(assignment, dict):
        evidence["policy_evaluation"] = {
            **assignment,
            "observed_reward": realized_policy_return,
            "reward_semantics": "realized_policy_return_decimal_fraction",
        }
        evidence["policy_evaluation_status"] = "available_confirmed_no_trade"
        semantics = dict(evidence.get("return_semantics") or {})
        semantics["realized_policy_return_basis"] = "confirmed_no_trade"
        evidence["return_semantics"] = semantics
    digest = envelope_sha256({"source_outcome_id": outcome.id, "evidence": evidence})
    return LoopOutcomeEnvelope.model_validate({
        **outcome.model_dump(mode="json"),
        "id": f"quant_outcome_linked_{digest[:40]}",
        "realized_policy_return": realized_policy_return,
        "evidence": evidence,
    })


def _stage_and_deliver(
    outcomes: list[LoopOutcomeEnvelope],
    *,
    client: LoopClient,
    outbox: LoopOutbox,
    stage_only: bool,
) -> tuple[int, int]:
    staged = delivered = 0
    for outcome in outcomes:
        payload = outcome.model_dump(mode="json")
        item = outbox.stage(
            event_id=outcome.id,
            event_type="outcome",
            payload=payload,
            payload_sha256=envelope_sha256(payload),
        )
        staged += 1
        if stage_only:
            continue
        if item.status == "delivered":
            delivered += 1
            continue
        try:
            client.submit_outcome(outcome)
        except Exception as exc:
            outbox.mark_failed(outcome.id, error_code=type(exc).__name__)
            raise
        outbox.mark_delivered(outcome.id)
        delivered += 1
    return staged, delivered


def sync_due_outcomes(
    *,
    client: LoopClient,
    outbox: LoopOutbox,
    data_root: Path,
    as_of_date: date,
    observed_before: datetime,
    config: OutcomeReporterConfig,
    stage_only: bool = False,
    execution_index_path: Path | None = None,
    execution_index_sha256: str | None = None,
) -> OutcomeSyncSummary:
    if observed_before.tzinfo is None or observed_before.utcoffset() != UTC.utcoffset(
        observed_before
    ):
        raise ValueError("observed_before must be timezone-aware UTC")
    if config.approved_at_utc > observed_before:
        raise ValueError("Outcome cost-model approval cannot be in the future")
    event_assignments = client.list_event_outcome_assignments(market_scope=config.market_scope)
    strategy_assignments = client.list_outcome_assignments(market_scope=config.market_scope)
    if any(item.market_scope != config.market_scope for item in event_assignments) or any(
        item.market_scope != config.market_scope for item in strategy_assignments
    ):
        raise ValueError("Loop returned an Outcome assignment outside configured scope")
    daily_index = _load_daily_index(
        data_root,
        config=config,
        observed_before=observed_before,
    )
    due = staged = delivered = 0
    pending: list[PendingOutcome] = []
    event_outcomes: list[LoopOutcomeEnvelope] = []
    for event_assignment in event_assignments:
        for horizon in event_assignment.outstanding_horizons:
            outcome, reason = build_due_event_outcome(
                event_assignment,
                horizon=horizon,
                as_of_date=as_of_date,
                daily_index=daily_index,
                config=config,
            )
            if reason != "horizon_not_mature":
                due += 1
            if outcome is None:
                pending.append(
                    PendingOutcome(
                        decision_event_id=event_assignment.decision_event_id,
                        strategy_revision_id="",
                        horizon=horizon,
                        reason=reason,
                    )
                )
            else:
                event_outcomes.append(outcome)
    event_staged, event_delivered = _stage_and_deliver(
        event_outcomes,
        client=client,
        outbox=outbox,
        stage_only=stage_only,
    )
    staged += event_staged
    delivered += event_delivered
    execution_inputs = (
        load_execution_index(execution_index_path, execution_index_sha256)
        if strategy_assignments
        else []
    )
    strategy_outcomes: list[LoopOutcomeEnvelope] = []
    for strategy_assignment in strategy_assignments:
        for horizon in strategy_assignment.outstanding_horizons:
            outcome, reason = build_due_outcome(
                strategy_assignment,
                horizon=horizon,
                as_of_date=as_of_date,
                daily_index=daily_index,
                config=config,
            )
            if reason != "horizon_not_mature":
                due += 1
            if outcome is None:
                pending.append(
                    PendingOutcome(
                        decision_event_id=strategy_assignment.decision_event_id,
                        strategy_revision_id=strategy_assignment.strategy_revision_id,
                        horizon=horizon,
                        reason=reason,
                    )
                )
                continue
            if execution_index_path is not None:
                matches = [entry for entry in execution_inputs
                           if entry.trade_date == strategy_assignment.decision_trading_date
                           and entry.strategy_sha256 == strategy_assignment.strategy_sha256]
                if not matches:
                    pending.append(PendingOutcome(
                        decision_event_id=strategy_assignment.decision_event_id,
                        strategy_revision_id=strategy_assignment.strategy_revision_id,
                        horizon=horizon, reason="execution_date_strategy_evidence_unavailable",
                    ))
                    continue
                outcome = attach_factual_execution(outcome, **matches[0].attachment_args())
            strategy_outcomes.append(outcome)
    strategy_staged, strategy_delivered = _stage_and_deliver(
        strategy_outcomes,
        client=client,
        outbox=outbox,
        stage_only=stage_only,
    )
    staged += strategy_staged
    delivered += strategy_delivered
    return OutcomeSyncSummary(
        assignments=len(event_assignments) + len(strategy_assignments),
        event_assignments=len(event_assignments),
        strategy_assignments=len(strategy_assignments),
        due=due,
        staged=staged,
        delivered=delivered,
        pending=tuple(pending),
    )
