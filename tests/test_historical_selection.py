from __future__ import annotations

from datetime import UTC, date, datetime, time

from data_plane.calendar import build_xnys_schedule
from research.history import (
    catalyst_lock_asof_utc,
    premarket_data_cutoff_utc,
    premarket_decision_asof_utc,
    premarket_feature_cutoff_et,
    required_premarket_symbols,
    target_sessions,
)


def test_historical_targets_are_252_ordered_xnys_sessions() -> None:
    values = target_sessions(end_date=date(2026, 7, 16), sessions=252)
    assert len(values) == 252
    assert values[0] == date(2025, 7, 16)
    assert values[-1] == date(2026, 7, 16)
    assert list(values) == sorted(values)


def test_catalyst_lock_matches_beijing_0800_without_dst_guessing() -> None:
    assert catalyst_lock_asof_utc(date(2026, 7, 16)) == datetime(2026, 7, 16, 0, 0, tzinfo=UTC)
    assert premarket_decision_asof_utc(date(2026, 7, 16)) == datetime(
        2026, 7, 16, 12, 0, tzinfo=UTC
    )


def test_premarket_cutoff_preserves_beijing_decision_across_us_dst() -> None:
    assert premarket_data_cutoff_utc(date(2025, 10, 31)) == datetime(
        2025, 10, 31, 12, 0, tzinfo=UTC
    )
    assert premarket_feature_cutoff_et(date(2025, 10, 31)) == time(8, 0)
    assert premarket_data_cutoff_utc(date(2025, 11, 3)) == datetime(
        2025, 11, 3, 12, 0, tzinfo=UTC
    )
    assert premarket_feature_cutoff_et(date(2025, 11, 3)) == time(7, 0)


def test_delayed_sip_replay_is_explicit_instead_of_hardcoded() -> None:
    assert premarket_data_cutoff_utc(
        date(2025, 10, 31), provider_delay_minutes=15
    ) == datetime(2025, 10, 31, 11, 45, tzinfo=UTC)
    assert premarket_feature_cutoff_et(
        date(2025, 10, 31), provider_delay_minutes=15
    ) == time(7, 45)


def test_premarket_plan_reuses_only_required_lookback_sessions() -> None:
    schedule = build_xnys_schedule(date(2026, 6, 1), date(2026, 7, 16))
    dates = schedule.get_column("trade_date").tail(22).to_list()
    plan = required_premarket_symbols(
        {dates[-2]: ("AAA",), dates[-1]: ("BBB",)},
        schedule=schedule,
        history_sessions=20,
    )

    assert len(plan) == 22
    assert plan[dates[0]] == ("AAA",)
    assert plan[dates[1]] == ("AAA", "BBB")
    assert plan[dates[-1]] == ("BBB",)
    assert all(isinstance(value, tuple) for value in plan.values())
