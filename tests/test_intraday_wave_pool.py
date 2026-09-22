from research.intraday_wave_ranking import rank_forward_pool


def test_repeated_name_can_break_one_place_rank_gap() -> None:
    ranked = rank_forward_pool(
        [
            {"symbol": "NEW", "forward_rank": 1},
            {"symbol": "PERSIST", "forward_rank": 2},
        ],
        prior_waves=([{"symbol": "PERSIST"}],),
        limit=20,
    )

    assert [row["symbol"] for row in ranked] == ["PERSIST", "NEW"]
    assert ranked[0]["repeat_count"] == 1


def test_final_pool_counts_both_prior_waves_and_caps_at_top_ten() -> None:
    ranked = rank_forward_pool(
        [
            {"symbol": "A", "forward_rank": 1},
            {"symbol": "B", "forward_rank": 2},
            {"symbol": "C", "forward_rank": 3},
        ],
        prior_waves=(
            [{"symbol": "C"}],
            [{"symbol": "C"}],
        ),
        limit=2,
    )

    assert [row["symbol"] for row in ranked] == ["C", "A"]
    assert ranked[0]["repeat_count"] == 2
