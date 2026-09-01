from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl

from research.no_trade_review import build_no_trade_review
from research.pdca_agents import materialize_execution_memory

TRADE_DATE = date(2026, 8, 12)
OPEN = datetime(2026, 8, 12, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 8, 12, 20, 0, tzinfo=UTC)


def _episodes(*, triggered: bool, trigger_at: datetime | None = None) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["TEST"],
            "session_date": [TRADE_DATE],
            "selection_rank": [1],
            "signal_triggered": [triggered],
            "signal_reason": ["breakout" if triggered else "no_breakout_at_asof"],
            "trigger_ts_utc": [trigger_at],
            "outcome_label": ["sl" if triggered else "no_trigger"],
            "outcome_status": ["complete"],
            "gross_return": [-0.02 if triggered else None],
            "rth_high_return": [0.08],
            "rth_close_return": [0.03],
        }
    )


def _plans() -> pl.DataFrame:
    return pl.DataFrame(
        {"plan_id": ["plan-test"], "symbol": ["TEST"], "trade_date": [TRADE_DATE]}
    )


def test_review_does_not_blame_selection_when_runtime_ended_before_open() -> None:
    evaluations = pl.DataFrame(
        {
            "plan_id": ["plan-test"],
            "evaluation_count": [401],
            "observe_count": [0],
            "data_blocked_count": [401],
            "runtime_failure_count": [1],
            "submitted_order_count": [0],
            "first_observed_at_utc": [datetime(2026, 8, 12, 12, 12, tzinfo=UTC)],
            "last_observed_at_utc": [datetime(2026, 8, 12, 13, 9, tzinfo=UTC)],
        }
    )

    review = build_no_trade_review(
        plans=_plans(),
        episodes=_episodes(triggered=False),
        evaluations=evaluations,
        session_open_utc=OPEN,
        session_close_utc=CLOSE,
    ).row(0, named=True)

    assert review["execution_root_cause"] == "runtime_ended_before_open"
    assert review["selection_assessment"] == "not_attributable_in_live_runtime"
    assert review["requires_execution_fix"] is True


def test_review_flags_signal_execution_divergence_when_trigger_was_observable() -> None:
    trigger_at = datetime(2026, 8, 12, 14, 5, tzinfo=UTC)
    evaluations = pl.DataFrame(
        {
            "plan_id": ["plan-test"],
            "evaluation_count": [1200],
            "observe_count": [1100],
            "data_blocked_count": [100],
            "runtime_failure_count": [0],
            "submitted_order_count": [0],
            "first_observed_at_utc": [OPEN],
            "last_observed_at_utc": [CLOSE],
        }
    )

    review = build_no_trade_review(
        plans=_plans(),
        episodes=_episodes(triggered=True, trigger_at=trigger_at),
        evaluations=evaluations,
        session_open_utc=OPEN,
        session_close_utc=CLOSE,
    ).row(0, named=True)

    assert review["execution_root_cause"] == "signal_execution_divergence"
    assert review["selection_assessment"] == "triggered_loss"
    assert review["requires_execution_fix"] is True


def test_review_calls_complete_healthy_no_signal_a_strategy_no_trigger() -> None:
    evaluations = pl.DataFrame(
        {
            "plan_id": ["plan-test"],
            "evaluation_count": [2000],
            "observe_count": [2000],
            "data_blocked_count": [0],
            "runtime_failure_count": [0],
            "submitted_order_count": [0],
            "first_observed_at_utc": [OPEN],
            "last_observed_at_utc": [CLOSE],
        }
    )

    review = build_no_trade_review(
        plans=_plans(),
        episodes=_episodes(triggered=False),
        evaluations=evaluations,
        session_open_utc=OPEN,
        session_close_utc=CLOSE,
    ).row(0, named=True)

    assert review["execution_root_cause"] == "strategy_no_trigger"
    assert review["selection_assessment"] == "no_strategy_trigger"
    assert review["requires_execution_fix"] is False


def test_execution_gap_becomes_anonymous_non_production_memory() -> None:
    rows = [
        {
            "execution_root_cause": "runtime_ended_before_open",
            "requires_execution_fix": True,
            "evaluation_count": 401,
            "data_blocked_count": 401,
            "submitted_order_count": 0,
        }
    ]

    lessons = materialize_execution_memory(
        rows,
        trade_date=TRADE_DATE,
        source_record_ids=("review-snapshot",),
    )

    assert len(lessons) == 1
    assert lessons[0].category.value == "execution_gap"
    assert lessons[0].source_record_ids == ("review-snapshot",)
    assert "TEST" not in lessons[0].model_dump_json()
