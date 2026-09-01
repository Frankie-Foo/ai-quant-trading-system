"""Deterministic attribution for Paper sessions with no submitted orders."""

from __future__ import annotations

from datetime import datetime

import polars as pl

NO_TRADE_REVIEW_SCHEMA_VERSION = "paper_no_trade_review.v1"


def _require_columns(frame: pl.DataFrame, name: str, columns: set[str]) -> None:
    missing = columns - set(frame.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {sorted(missing)}")


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("execution count must be numeric")
    return int(value)


def _execution_cause(
    *,
    evaluation: dict[str, object] | None,
    episode: dict[str, object],
    session_open_utc: datetime,
    session_close_utc: datetime,
) -> str:
    if evaluation is None:
        return "execution_evidence_missing"
    submitted = _integer(evaluation["submitted_order_count"])
    if submitted:
        return "order_activity_recorded"
    last_observed = evaluation["last_observed_at_utc"]
    if not isinstance(last_observed, datetime):
        return "execution_evidence_invalid"
    if last_observed < session_open_utc:
        return "runtime_ended_before_open"
    triggered = bool(episode["signal_triggered"])
    trigger_at = episode.get("trigger_ts_utc")
    if triggered:
        if isinstance(trigger_at, datetime) and last_observed < trigger_at:
            return "execution_unavailable_at_trigger"
        evaluations = _integer(evaluation["evaluation_count"])
        blocked = _integer(evaluation["data_blocked_count"])
        if evaluations and evaluations == blocked:
            return "market_data_blocked"
        return "signal_execution_divergence"
    if last_observed < session_close_utc:
        return "execution_coverage_incomplete"
    return "strategy_no_trigger"


def _selection_assessment(*, episode: dict[str, object], execution_root_cause: str) -> str:
    if execution_root_cause in {
        "execution_evidence_missing",
        "execution_evidence_invalid",
        "runtime_ended_before_open",
        "execution_coverage_incomplete",
    }:
        return "not_attributable_in_live_runtime"
    if not bool(episode["signal_triggered"]):
        return "no_strategy_trigger"
    outcome = str(episode["outcome_label"])
    if outcome == "tp":
        return "triggered_win"
    if outcome == "sl":
        return "triggered_loss"
    if outcome == "unavailable":
        return "triggered_outcome_unavailable"
    return f"triggered_{outcome}"


def build_no_trade_review(
    *,
    plans: pl.DataFrame,
    episodes: pl.DataFrame,
    evaluations: pl.DataFrame,
    session_open_utc: datetime,
    session_close_utc: datetime,
) -> pl.DataFrame:
    """Attribute no-order outcomes without confusing runtime failure with bad selection."""

    if session_open_utc.tzinfo is None or session_close_utc.tzinfo is None:
        raise ValueError("session timestamps must be timezone-aware")
    if session_close_utc <= session_open_utc:
        raise ValueError("session close must be after open")
    _require_columns(plans, "plans", {"plan_id", "symbol", "trade_date"})
    _require_columns(
        episodes,
        "episodes",
        {
            "symbol",
            "session_date",
            "selection_rank",
            "signal_triggered",
            "signal_reason",
            "trigger_ts_utc",
            "outcome_label",
            "outcome_status",
            "gross_return",
            "rth_high_return",
            "rth_close_return",
        },
    )
    _require_columns(
        evaluations,
        "evaluations",
        {
            "plan_id",
            "evaluation_count",
            "observe_count",
            "data_blocked_count",
            "runtime_failure_count",
            "submitted_order_count",
            "first_observed_at_utc",
            "last_observed_at_utc",
        },
    )
    episode_by_symbol = {str(row["symbol"]): row for row in episodes.iter_rows(named=True)}
    evaluation_by_plan = {str(row["plan_id"]): row for row in evaluations.iter_rows(named=True)}
    rows: list[dict[str, object]] = []
    for plan in plans.iter_rows(named=True):
        symbol = str(plan["symbol"])
        if symbol not in episode_by_symbol:
            raise ValueError(f"episode unavailable for Paper plan: {symbol}")
        episode = episode_by_symbol[symbol]
        evaluation = evaluation_by_plan.get(str(plan["plan_id"]))
        cause = _execution_cause(
            evaluation=evaluation,
            episode=episode,
            session_open_utc=session_open_utc,
            session_close_utc=session_close_utc,
        )
        execution_fix = cause not in {"strategy_no_trigger", "order_activity_recorded"}
        rows.append(
            {
                "plan_id": str(plan["plan_id"]),
                "symbol": symbol,
                "session_date": plan["trade_date"],
                "selection_rank": int(episode["selection_rank"]),
                "evaluation_count": (_integer(evaluation["evaluation_count"]) if evaluation else 0),
                "observe_count": (_integer(evaluation["observe_count"]) if evaluation else 0),
                "data_blocked_count": (
                    _integer(evaluation["data_blocked_count"]) if evaluation else 0
                ),
                "runtime_failure_count": (
                    _integer(evaluation["runtime_failure_count"]) if evaluation else 0
                ),
                "submitted_order_count": (
                    _integer(evaluation["submitted_order_count"]) if evaluation else 0
                ),
                "first_observed_at_utc": (
                    evaluation["first_observed_at_utc"] if evaluation else None
                ),
                "last_observed_at_utc": (
                    evaluation["last_observed_at_utc"] if evaluation else None
                ),
                "signal_triggered": bool(episode["signal_triggered"]),
                "signal_reason": str(episode["signal_reason"]),
                "trigger_ts_utc": episode.get("trigger_ts_utc"),
                "outcome_label": str(episode["outcome_label"]),
                "outcome_status": str(episode["outcome_status"]),
                "gross_return": episode.get("gross_return"),
                "rth_high_return": episode.get("rth_high_return"),
                "rth_close_return": episode.get("rth_close_return"),
                "execution_root_cause": cause,
                "selection_assessment": _selection_assessment(
                    episode=episode, execution_root_cause=cause
                ),
                "requires_execution_fix": execution_fix,
                "production_change_allowed": False,
                "review_provenance": (f"research.no_trade_review:{NO_TRADE_REVIEW_SCHEMA_VERSION}"),
            }
        )
    return pl.DataFrame(rows).sort("selection_rank", "symbol")
