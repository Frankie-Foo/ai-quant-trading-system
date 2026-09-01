from datetime import date

import polars as pl

from research.modern_momentum_forward import select_forward_pool


def test_forward_pool_enforces_market_cap_and_ranks_hard_catalyst_first() -> None:
    gates = pl.DataFrame(
        {
            "symbol": ["HARD", "SOFT", "SMALL"],
            "session_date": [date(2026, 8, 18)] * 3,
            "market_cap": [None, None, None],
            "rvol": [2.0, 5.0, 10.0],
            "premarket_return": [0.01, 0.03, 0.20],
            "catalyst_categories": [["earnings"], ["other_material"], ["earnings"]],
            "current_halt": [False] * 3,
            "luld_risk": [False] * 3,
        }
    )

    result = select_forward_pool(
        gates,
        market_caps={"HARD": 2e9, "SOFT": 3e9, "SMALL": 5e8},
    )

    assert result.get_column("symbol").to_list() == ["HARD", "SOFT"]
    assert result.get_column("forward_rank").to_list() == [1, 2]
