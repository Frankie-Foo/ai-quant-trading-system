from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from kernel.config import load_config
from research.evolution import evaluate_proposal
from research.postmortem import build_trading_episode
from research.postmortem_agents import (
    CriticReview,
    ResearchReview,
    validate_evidence_symbols,
)
from research.program_review import (
    FindingCode,
    ProgramReviewStatus,
    ReviewPolicy,
    build_program_review,
)
from schedule.monthly_evolution import is_first_xnys_session
from schedule.postmarket import postmarket_due
from schedule.state import JobLedger, JobStatus

OPEN = datetime(2026, 7, 20, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 7, 20, 20, 0, tzinfo=UTC)


def _selection() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["FAST", "QUIET"],
            "session_date": [date(2026, 7, 20)] * 2,
            "selection_rank": [1, 2],
            "pass_gate": [True, True],
            "rvol": [8.0, 4.0],
            "price": [10.0, 20.0],
            "adv_usd": [10_000_000.0, 20_000_000.0],
            "market_cap": [2_500_000_000.0, 12_000_000_000.0],
            "tier": ["mid", "large"],
            "beta": [1.8, 1.6],
            "atr_pct": [0.05, 0.04],
            "event_count": [1, 1],
            "catalyst_categories": [["clinical"], ["general_news"]],
            "evidence_event_ids": [["news:fast"], ["news:quiet"]],
            "evidence_sources": [["massive.news"], ["massive.news"]],
        }
    )


def _signals() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["FAST", "QUIET"],
            "triggered": [True, False],
            "reason": ["triggered", "opening_range_not_bullish"],
            "opening_range_high": [10.45, 20.4],
            "opening_range_low": [9.9, 19.8],
            "opening_range_open": [10.0, 20.2],
            "opening_range_close": [10.4, 20.0],
            "trigger_ts_utc": [OPEN + timedelta(minutes=5), None],
            "entry_ts_utc": [OPEN + timedelta(minutes=6), None],
            "entry_px": [10.6, None],
            "provenance": ["signal:fast", "signal:quiet"],
            "session_date": [date(2026, 7, 20)] * 2,
        }
    )


def _bars() -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    fast = [
        (10.0, 10.2, 9.9, 10.1, 10.05),
        (10.1, 10.3, 10.0, 10.2, 10.15),
        (10.2, 10.4, 10.1, 10.3, 10.25),
        (10.3, 10.35, 10.2, 10.25, 10.28),
        (10.25, 10.45, 10.2, 10.4, 10.33),
        (10.4, 10.6, 10.35, 10.55, 10.50),
        (10.55, 10.7, 10.5, 10.65, 10.60),
        (10.65, 11.7, 10.6, 11.6, 11.40),
    ]
    quiet = [
        (20.2, 20.3, 20.0, 20.1, 20.15),
        (20.1, 20.4, 20.0, 20.2, 20.20),
        (20.2, 20.3, 19.9, 20.0, 20.10),
        (20.0, 20.2, 19.8, 19.9, 20.00),
        (19.9, 20.1, 19.8, 20.0, 19.95),
    ]
    for symbol, values in (("FAST", fast), ("QUIET", quiet)):
        for minute, (open_, high, low, close, vwap) in enumerate(values):
            rows.append(
                {
                    "symbol": symbol,
                    "ts_utc": OPEN + timedelta(minutes=minute),
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "vwap": vwap,
                    "volume": 100_000,
                }
            )
    return pl.DataFrame(rows).with_columns(pl.col("ts_utc").cast(pl.Datetime("ms", "UTC")))


def test_episode_uses_kernel_label_and_does_not_invent_net_costs() -> None:
    scores = pl.DataFrame(
        {
            "symbol": ["FAST", "QUIET"],
            "raw_probability": [0.7, 0.1],
            "calibration_status": ["unapproved_shadow"] * 2,
            "approved_for_kernel": [False, False],
            "model_id": ["deepseek-v4-pro"] * 2,
            "prompt_sha256": ["a" * 64, "b" * 64],
        }
    )
    episode = build_trading_episode(
        selection=_selection(),
        signals=_signals(),
        bars=_bars(),
        catalyst_scores=scores,
        trade_date=date(2026, 7, 20),
        session_open_utc=OPEN,
        session_close_utc=CLOSE,
        is_half_day=False,
        cfg=load_config("config.yaml"),
    )
    fast = episode.filter(pl.col("symbol") == "FAST").row(0, named=True)
    quiet = episode.filter(pl.col("symbol") == "QUIET").row(0, named=True)

    assert fast["outcome_label"] == "tp"
    assert fast["gross_return"] == pytest.approx((11.6 / 10.6) - 1)
    assert fast["net_return"] is None
    assert fast["net_return_status"] == "unavailable_missing_quote_spread"
    assert fast["model_score_approved"] is False
    assert quiet["outcome_label"] == "no_trigger"
    assert episode.get_column("symbol").n_unique() == 2


def test_research_and_critic_contracts_are_strict() -> None:
    review = ResearchReview.model_validate(
        {
            "summary": "One falsifiable issue was found.",
            "hypotheses": [
                {
                    "title": "Raise evidence quality",
                    "target_component": "catalyst_filter",
                    "mechanism": "Low-information releases create false candidates.",
                    "proposed_change": "Require a material operating event.",
                    "falsification_test": "Compare unchanged chronological folds.",
                    "evidence_symbols": ["FAST"],
                }
            ],
        }
    )
    critic = CriticReview.model_validate(
        {
            "verdict": "eligible_for_experiment",
            "rationale": "The claim is testable but not production-ready.",
            "leakage_risks": [],
            "overfit_risks": ["single_session"],
            "unsupported_claims": [],
            "required_evidence": ["twenty_sessions"],
        }
    )
    assert review.hypotheses[0].target_component == "catalyst_filter"
    assert critic.verdict == "eligible_for_experiment"
    with pytest.raises(ValueError):
        ResearchReview.model_validate(
            {"summary": "bad", "hypotheses": [], "production_override": True}
        )


def test_evolution_gate_never_promotes_and_requires_enough_episodes() -> None:
    review = ResearchReview.model_validate(
        {
            "summary": "Testable.",
            "hypotheses": [
                {
                    "title": "RVOL grid",
                    "target_component": "rvol_gate",
                    "mechanism": "The threshold may be regime-dependent.",
                    "proposed_change": "Evaluate an allowlisted RVOL grid.",
                    "falsification_test": "Purged walk-forward versus the champion.",
                    "evidence_symbols": ["FAST"],
                }
            ],
        }
    )
    critic = CriticReview.model_validate(
        {
            "verdict": "eligible_for_experiment",
            "rationale": "Safe to test only.",
            "leakage_risks": [],
            "overfit_risks": ["small_sample"],
            "unsupported_claims": [],
            "required_evidence": ["more_sessions"],
        }
    )
    waiting = evaluate_proposal(
        review,
        critic,
        episode_count=1,
        labeled_trade_count=1,
        min_episodes=20,
        min_labeled_trades=20,
    )
    eligible = evaluate_proposal(
        review,
        critic,
        episode_count=20,
        labeled_trade_count=20,
        min_episodes=20,
        min_labeled_trades=20,
    )
    assert waiting.status == "waiting_for_minimum_episodes"
    assert waiting.approved_for_production is False
    assert eligible.status == "eligible_for_sandbox_experiment"
    assert eligible.approved_for_production is False


def test_empty_or_unbound_agent_hypotheses_cannot_advance() -> None:
    empty = ResearchReview(summary="Facts are insufficient.", hypotheses=())
    critic = CriticReview.model_validate(
        {
            "verdict": "eligible_for_experiment",
            "rationale": "Nothing to admit.",
            "leakage_risks": [],
            "overfit_risks": [],
            "unsupported_claims": [],
            "required_evidence": [],
        }
    )
    decision = evaluate_proposal(
        empty,
        critic,
        episode_count=100,
        labeled_trade_count=100,
        min_episodes=20,
        min_labeled_trades=20,
    )
    assert decision.status == "no_actionable_hypothesis"

    review = ResearchReview.model_validate(
        {
            "summary": "Bad evidence binding.",
            "hypotheses": [
                {
                    "title": "Unknown symbol",
                    "target_component": "data_quality",
                    "mechanism": "An unknown symbol was cited by the model.",
                    "proposed_change": "Do not accept unbound evidence symbols.",
                    "falsification_test": "Check symbols against the episode set.",
                    "evidence_symbols": ["MADEUP"],
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="outside the episode"):
        validate_evidence_symbols(review, {"FAST"})


def test_job_ledger_is_idempotent_and_retries_failures(tmp_path: Path) -> None:
    ledger = JobLedger(tmp_path / "jobs.sqlite3")
    target = date(2026, 7, 20)

    first = ledger.acquire("postmarket_review", target, "v1")
    assert first is not None
    assert ledger.acquire("postmarket_review", target, "v1") is None
    ledger.fail(first, error_code="RuntimeError")
    second = ledger.acquire("postmarket_review", target, "v1")
    assert second is not None
    ledger.complete(second, artifact_ids=("episode-1", "review-1"))
    assert ledger.acquire("postmarket_review", target, "v1") is None
    record = ledger.get("postmarket_review", target, "v1")
    assert record is not None
    assert record.status is JobStatus.SUCCEEDED
    assert record.attempts == 2
    assert record.artifact_ids == ("episode-1", "review-1")


def test_stale_job_lease_cannot_complete_a_new_attempt(tmp_path: Path) -> None:
    ledger = JobLedger(tmp_path / "jobs.sqlite3", stale_after=timedelta(0))
    target = date(2026, 7, 20)

    stale = ledger.acquire("postmarket_review", target, "v2")
    assert stale is not None
    current = ledger.acquire("postmarket_review", target, "v2")
    assert current is not None
    with pytest.raises(RuntimeError, match="lease is not current"):
        ledger.complete(stale, artifact_ids=("stale",))
    ledger.complete(current, artifact_ids=("current",))


def _review_frame(*, sessions: int, with_net_costs: bool) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for index in range(sessions):
        label = ("tp", "sl", "time")[index % 3]
        gross = (0.02, -0.01, 0.005)[index % 3]
        rows.append(
            {
                "symbol": f"S{index:03d}",
                "session_date": date(2026, 6, 1) + timedelta(days=index),
                "signal_triggered": True,
                "outcome_label": label,
                "outcome_status": "complete",
                "gross_return": gross,
                "net_return": gross - 0.002 if with_net_costs else None,
                "net_return_status": (
                    "complete" if with_net_costs else "unavailable_missing_quote_spread"
                ),
            }
        )
    return pl.DataFrame(rows)


def test_program_review_blocks_missing_paths_and_costs_without_an_llm() -> None:
    frame = _review_frame(sessions=20, with_net_costs=False)
    frame = frame.with_columns(
        pl.when(pl.col("symbol") == "S019")
        .then(pl.lit("unavailable"))
        .otherwise(pl.col("outcome_label"))
        .alias("outcome_label"),
        pl.when(pl.col("symbol") == "S019")
        .then(pl.lit("incomplete_minute_path"))
        .otherwise(pl.col("outcome_status"))
        .alias("outcome_status"),
    )

    review = build_program_review(
        current_episode=frame.tail(1),
        episode_history=frame,
        policy=ReviewPolicy(
            minimum_episodes=20,
            minimum_labeled_trades=20,
            minimum_net_labeled_trades=20,
        ),
    )

    assert review.status is ProgramReviewStatus.BLOCKED_DATA_QUALITY
    assert review.approved_for_production is False
    assert review.llm_research_allowed is False
    assert {finding.code for finding in review.findings} >= {
        FindingCode.INCOMPLETE_TRIGGER_PATHS,
        FindingCode.NET_COSTS_UNAVAILABLE,
    }


def test_program_review_creates_only_allowlisted_sandbox_spec() -> None:
    frame = _review_frame(sessions=20, with_net_costs=True)
    review = build_program_review(
        current_episode=frame.tail(1),
        episode_history=frame,
        policy=ReviewPolicy(
            minimum_episodes=20,
            minimum_labeled_trades=20,
            minimum_net_labeled_trades=20,
        ),
    )

    assert review.status is ProgramReviewStatus.ELIGIBLE_FOR_SANDBOX_EXPERIMENT
    assert review.llm_research_allowed is True
    assert review.approved_for_production is False
    assert len(review.sandbox_experiments) == 1
    experiment = review.sandbox_experiments[0]
    assert experiment.experiment_id == "rvol_threshold_sensitivity.v1"
    assert experiment.attempted_configurations == 3
    assert experiment.production_eligible is False


def test_program_review_rejects_future_episode_history() -> None:
    frame = _review_frame(sessions=2, with_net_costs=True)
    with pytest.raises(ValueError, match="future session"):
        build_program_review(
            current_episode=frame.head(1),
            episode_history=frame,
        )


def test_postmarket_time_gate_uses_exchange_close_and_provider_delay() -> None:
    assert postmarket_due(date(2026, 7, 20), datetime(2026, 7, 20, 20, 19, tzinfo=UTC)) is False
    assert postmarket_due(date(2026, 7, 20), datetime(2026, 7, 20, 20, 20, tzinfo=UTC)) is True


def test_monthly_evolution_runs_only_on_first_xnys_session() -> None:
    assert is_first_xnys_session(date(2026, 7, 1)) is True
    assert is_first_xnys_session(date(2026, 7, 2)) is False
