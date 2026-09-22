from __future__ import annotations

import pytest

from research.intraday_wave_ranking import rank_forward_pool, rank_wave


@pytest.mark.parametrize("rank", [True, 1.5, float("inf"), float("nan"), None, {}, -1])
def test_invalid_forward_rank_is_not_truncated_or_coerced(rank: object) -> None:
    with pytest.raises(ValueError, match="forward_rank"):
        rank_forward_pool([{"symbol": "AAA", "forward_rank": rank}], prior_waves=(), limit=10)


@pytest.mark.parametrize("score", [True, {}, None, float("inf")])
def test_invalid_base_score_is_rejected(score: object) -> None:
    with pytest.raises(ValueError, match="base_score"):
        rank_wave([{"symbol": "AAA", "base_score": score}], prior_waves=(), limit=10)


def test_repeated_symbols_receive_a_prior_wave_bonus() -> None:
    ranked = rank_wave(
        [
            {"symbol": "AAA", "base_score": 7.0},
            {"symbol": "BBB", "base_score": 7.5},
        ],
        prior_waves=([{"symbol": "AAA", "base_score": 8.0}],),
        limit=20,
    )

    assert [row["symbol"] for row in ranked] == ["AAA", "BBB"]
    assert ranked[0]["repeat_count"] == 1
    assert ranked[0]["weighted_score"] == 8.0


def test_two_prior_wave_appearances_receive_two_bonuses() -> None:
    ranked = rank_wave(
        [
            {"symbol": "AAA", "base_score": 6.0},
            {"symbol": "BBB", "base_score": 7.5},
        ],
        prior_waves=(
            [{"symbol": "AAA", "base_score": 8.0}],
            [{"symbol": "AAA", "base_score": 7.0}],
        ),
        limit=10,
    )

    assert [row["symbol"] for row in ranked] == ["AAA", "BBB"]
    assert ranked[0]["repeat_count"] == 2
    assert ranked[0]["weighted_score"] == 8.0


def test_rank_wave_caps_result_and_breaks_ties_by_symbol() -> None:
    ranked = rank_wave(
        [
            {"symbol": "ZZZ", "base_score": 5.0},
            {"symbol": "AAA", "base_score": 5.0},
            {"symbol": "BBB", "base_score": 4.0},
        ],
        prior_waves=(),
        limit=2,
    )

    assert [row["symbol"] for row in ranked] == ["AAA", "ZZZ"]
