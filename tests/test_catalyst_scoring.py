from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from research.catalyst_scoring import (
    assert_post_training_evaluation,
    parse_probability,
    score_intraday_continuation,
)


def test_probability_parser_accepts_only_one_bounded_number() -> None:
    assert parse_probability("0.73") == pytest.approx(0.73)
    with pytest.raises(ValueError):
        parse_probability("Probability: 0.73")
    with pytest.raises(ValueError):
        parse_probability("1.2")


def test_catalyst_score_records_prompt_model_and_evidence_provenance() -> None:
    result = score_intraday_continuation(
        symbol="FAST",
        evidence=[
            ("news:1", "Fast Corp wins a material multi-year customer contract"),
            ("sec:2", "Fast Corp files an 8-K describing the agreement"),
        ],
        asof_utc=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        model_id="provider-model-2026-01",
        score_fn=lambda prompt: "0.73",
    )
    assert result.probability == pytest.approx(0.73)
    assert result.temperature == 0.0
    assert result.evidence_ids == ("news:1", "sec:2")
    assert len(result.prompt_sha256) == 64
    assert "0.73" not in result.provenance


def test_evaluation_window_must_follow_model_training_cutoff() -> None:
    assert_post_training_evaluation(
        model_training_cutoff=date(2025, 12, 31),
        evaluation_start=date(2026, 1, 1),
    )
    with pytest.raises(ValueError, match="strictly after"):
        assert_post_training_evaluation(
            model_training_cutoff=date(2025, 12, 31),
            evaluation_start=date(2025, 12, 31),
        )
