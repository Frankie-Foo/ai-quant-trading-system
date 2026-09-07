"""Deterministic, point-in-time delayed Outcome generation for Loop governance."""

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
    OUTCOME_EXCESS_FORMULA,
    OUTCOME_HORIZON_SESSIONS,
    LoopOutcomeAssignment,
    LoopOutcomeEnvelope,
    OutcomeReporterConfig,
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
    due: int
    staged: int
    delivered: int
    pending: tuple[PendingOutcome, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignments": self.assignments,
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
    for path in (data_root / "accepted").glob(
        f"{config.price_source}-*/data.parquet"
    ):
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
            raise ValueError(
                f"daily snapshot {snapshot.dataset_id} misses {sorted(missing)}"
            )
        dates = frame.get_column("trade_date").cast(pl.Date).unique().to_list()
        if len(dates) != 1 or not isinstance(dates[0], date):
            raise ValueError(
                f"daily snapshot {snapshot.dataset_id} must contain one trading date"
            )
        adjustments = {
            str(value) for value in frame.get_column("adjustment").unique().to_list()
        }
        if adjustments != {config.adjustment}:
            raise ValueError(
                f"daily snapshot {snapshot.dataset_id} adjustment mismatch"
            )
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
        "horizon": horizon,
        "snapshot_ids": snapshot_ids,
        "cost_model_version": config.cost_model_version,
        "return_semantics": "close_to_close_split_adjusted.v1",
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"quant_outcome_v2_{digest[:40]}"


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
    instrument_rows = [
        _price_row(snapshot, assignment.instrument) for snapshot in snapshots
    ]
    if any(row is None for row in instrument_rows):
        return None, f"instrument_bar_missing_or_halted:{assignment.instrument}"
    benchmark_rows = [
        _price_row(snapshot, config.benchmark_symbol) for snapshot in snapshots
    ]
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
    transaction_cost = (
        config.transaction_cost_bps_round_trip / 10_000 if enters_position else 0.0
    )
    slippage = config.slippage_bps_round_trip / 10_000 if enters_position else 0.0
    excess_return = (
        strategy_return - benchmark_return - transaction_cost - slippage
    )
    counterfactual_net_alpha = (
        instrument_return
        - benchmark_return
        - config.transaction_cost_bps_round_trip / 10_000
        - config.slippage_bps_round_trip / 10_000
    )
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
        "evaluation_role": assignment.evaluation_role,
        "point_in_time_guard_passed": True,
        "decision_trading_date": assignment.decision_trading_date.isoformat(),
        "horizon_end_trading_date": horizon_date.isoformat(),
        "horizon_end_market_close_utc": horizon_close.astimezone(UTC).isoformat(),
        "trading_session_dates": [
            row["trade_date"].isoformat() for row in selected_sessions
        ],
        "trading_calendar": {
            "name": "XNYS",
            "source": calendar_source,
            "version": calendar_version,
        },
        "benchmark_id": config.benchmark_symbol,
        "price_snapshot_ids": snapshot_ids,
        "return_semantics": {
            "unit": "decimal_fraction",
            "method": "close_to_close_split_adjusted",
            "strategy_return_basis": "gross_before_costs",
            "excess_return_formula": OUTCOME_EXCESS_FORMULA,
        },
        "target_verdict": target_verdict,
        "observed_verdict": assignment.observed_verdict,
        "instrument_return": instrument_return,
        "counterfactual_net_excess_return": counterfactual_net_alpha,
        "counterfactual_max_drawdown": counterfactual_drawdown,
        "direction_correctness_rule": correctness_rule,
        "watch_neutral_band_bps": config.watch_neutral_band_bps,
        "cost_model_version": config.cost_model_version,
        "cost_model_approved_by": config.approved_by,
        "cost_model_approved_at_utc": config.approved_at_utc.isoformat(),
        "synthetic": False,
    }
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
        strategy_return=strategy_return,
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


def sync_due_outcomes(
    *,
    client: LoopClient,
    outbox: LoopOutbox,
    data_root: Path,
    as_of_date: date,
    observed_before: datetime,
    config: OutcomeReporterConfig,
    stage_only: bool = False,
) -> OutcomeSyncSummary:
    if (
        observed_before.tzinfo is None
        or observed_before.utcoffset() != UTC.utcoffset(observed_before)
    ):
        raise ValueError("observed_before must be timezone-aware UTC")
    if config.approved_at_utc > observed_before:
        raise ValueError("Outcome cost-model approval cannot be in the future")
    assignments = client.list_outcome_assignments(market_scope=config.market_scope)
    if any(item.market_scope != config.market_scope for item in assignments):
        raise ValueError("Loop returned an Outcome assignment outside configured scope")
    daily_index = _load_daily_index(
        data_root,
        config=config,
        observed_before=observed_before,
    )
    due = staged = delivered = 0
    pending: list[PendingOutcome] = []
    for assignment in assignments:
        for horizon in assignment.outstanding_horizons:
            outcome, reason = build_due_outcome(
                assignment,
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
                        decision_event_id=assignment.decision_event_id,
                        strategy_revision_id=assignment.strategy_revision_id,
                        horizon=horizon,
                        reason=reason,
                    )
                )
                continue
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
    return OutcomeSyncSummary(
        assignments=len(assignments),
        due=due,
        staged=staged,
        delivered=delivered,
        pending=tuple(pending),
    )
