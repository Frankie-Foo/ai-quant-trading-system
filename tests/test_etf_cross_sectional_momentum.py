from datetime import date, timedelta

from research.etf_cross_sectional_momentum import (
    MomentumConfig,
    backtest,
    build_target_weights,
)


def _dates(count: int) -> list[date]:
    start = date(2024, 1, 1)
    return [start + timedelta(days=index) for index in range(count)]


def test_signal_skips_the_most_recent_observations() -> None:
    dates = _dates(8)
    prices = {
        "A": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 1.0, 1.0],
        "B": [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 20.0, 20.0],
    }
    config = MomentumConfig(lookback_days=5, skip_days=2, holdings=1, max_weight=1.0)

    weights = build_target_weights(dates, prices, config, rebalance_indexes={7})

    assert weights[7] == {"A": 1.0, "B": 0.0}


def test_backtest_executes_signal_on_the_following_return() -> None:
    dates = _dates(5)
    prices = {"A": [100.0, 100.0, 100.0, 110.0, 121.0]}
    targets = [
        {"A": 0.0},
        {"A": 0.0},
        {"A": 1.0},
        {"A": 1.0},
        {"A": 1.0},
    ]

    result = backtest(dates, prices, targets, cost_bps=0.0)

    assert result.daily_returns[2] == 0.0
    assert round(result.daily_returns[3], 8) == 0.1
    assert round(result.daily_returns[4], 8) == 0.1


def test_turnover_cost_charges_initial_entry_and_rotation() -> None:
    dates = _dates(4)
    prices = {"A": [100.0] * 4, "B": [100.0] * 4}
    targets = [
        {"A": 0.0, "B": 0.0},
        {"A": 1.0, "B": 0.0},
        {"A": 0.0, "B": 1.0},
        {"A": 0.0, "B": 1.0},
    ]

    result = backtest(dates, prices, targets, cost_bps=5.0)

    assert result.turnover[2] == 1.0
    assert result.turnover[3] == 2.0
    assert result.daily_returns[2] == -0.0005
    assert result.daily_returns[3] == -0.001


def test_absolute_filter_keeps_cash_when_every_score_is_negative() -> None:
    dates = _dates(8)
    prices = {
        "A": [10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0],
        "B": [10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0],
    }
    config = MomentumConfig(
        lookback_days=5,
        skip_days=1,
        holdings=2,
        max_weight=0.5,
        require_positive_momentum=True,
    )

    weights = build_target_weights(dates, prices, config, rebalance_indexes={7})

    assert weights[7] == {"A": 0.0, "B": 0.0}
