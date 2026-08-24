from datetime import UTC, datetime, timedelta

import polars as pl

from scripts.monitor_intraday_attack import (
    ACTIVE_PLANS,
    ACTIVE_SYMBOLS,
    ETF_PLANS,
    AttackPlan,
    Candidate,
    _buy_signal,
    _status_summary_message,
    box_within_atr,
    capacity_check,
    clean_breakout_ready,
    confirmed_retest_ready,
    event_pullback_ready,
    observed_trade_minute_bars,
    rolling_box,
    select_one_per_theme,
    size_candidate,
)


def test_today_plan_preserves_role_specific_volume_and_risk_caps() -> None:
    plans = {plan.symbol: plan for plan in ACTIVE_PLANS}

    assert plans["AVGO"].minimum_volume_ratio == 1.8
    assert plans["LITE"].minimum_volume_ratio == 1.8
    assert plans["LUNR"].minimum_volume_ratio == 2.5
    assert plans["LUNR"].max_stop_fraction == 0.015
    assert plans["NEM"].upstream_vwap_symbols == ("GLD", "GDX")
    assert plans["CF"].requires_capacity_check is True
    assert plans["DE"].requires_capacity_check is True


def test_scout_size_uses_both_capital_and_risk_budget() -> None:
    candidate = Candidate(
        AttackPlan("TEST", 1, "clean", "SPY", capital_limit=1_000_000, risk_budget=10_000),
        100,
        98,
        101,
        (1.0, 1.5, -1),
        100,
        99,
        99,
        1.5,
        "opening_h30_scout",
    )

    size = size_candidate(candidate)

    assert size["stage"] == "GO_SCOUT"
    assert size["target_shares"] == 1250
    assert size["incremental_shares"] == 1250
    assert size["target_risk"] == 2500


def test_confirm_size_emits_only_incremental_add_after_scout() -> None:
    candidate = Candidate(
        AttackPlan("CF", 1, "clean", "XLB", capital_limit=1_000_000, risk_budget=10_000),
        100,
        98,
        101,
        (1.0, 1.5, -1),
        100,
        99,
        99,
        1.5,
        "confirmed_retest",
    )

    size = size_candidate(candidate)

    assert size["stage"] == "GO_CONFIRM"
    assert size["target_shares"] == 3000
    assert size["incremental_shares"] == 1750
    assert size["target_risk"] == 6000


def test_confirm_signal_states_incremental_add_and_fill_requirement() -> None:
    signal = _buy_signal(
        Candidate(
            AttackPlan("CF", 1, "clean", "XLB", capital_limit=1_000_000, risk_budget=10_000),
            100,
            98,
            101,
            (1.0, 1.5, -1),
            100,
            99,
            99,
            1.5,
            "confirmed_retest",
        )
    )

    assert "加仓目标" in signal.message
    assert "侦察仓已真实成交" in signal.message
    assert ":go_confirm:CF:" in signal.dedupe_key


def test_capacity_check_limits_scout_to_configured_breakout_participation() -> None:
    plan = AttackPlan("CF", 1, "clean", "XLB", capital_limit=600_000, risk_budget=5_000)

    result = capacity_check(
        plan,
        ask=100.0,
        stop=99.0,
        breakout_close=100.0,
        breakout_volume=20_000.0,
    )

    assert result["scout_notional"] == 125_000.0
    assert result["breakout_dollar_volume"] == 2_000_000.0
    assert result["capacity_notional"] == 160_000.0
    assert result["passes"] is True


def test_capacity_check_uses_incremental_notional_for_confirm_add() -> None:
    result = capacity_check(
        AttackPlan("CF", 1, "clean", "XLB", capital_limit=1_000_000, risk_budget=10_000),
        ask=100.0,
        stop=98.0,
        breakout_close=100.0,
        breakout_volume=20_000.0,
        stage="GO_CONFIRM",
    )

    assert result["action_notional"] == 175_000.0
    assert result["passes"] is False


def test_today_etfs_are_confirmation_proxies_not_trade_candidates() -> None:
    assert ETF_PLANS == ()


def test_half_hour_summary_groups_statuses_by_actionability() -> None:
    message = _status_summary_message(
        {
            "FCX": {"blocker": "vwap_not_rising"},
            "HOOD": {"blocker": "secondary_box_breakout_pending"},
            "AVGO": {"blocker": "upstream_below_vwap"},
            "LITE": {"blocker": "new_standard_entries_closed_after_1430"},
        },
        selected=[],
        local=datetime(2026, 8, 21, 10, 30, tzinfo=UTC),
    )

    assert "强观察：HOOD（二级箱未有效突破）" in message
    assert "不能买：AVGO（上游ETF未站稳VWAP）" in message
    assert "LITE（14:30后禁止标准新开仓）" in message
    assert "继续观察：FCX（VWAP未上行）" in message


def test_selection_keeps_only_strongest_same_theme() -> None:
    candidates = [
        Candidate(
            AttackPlan("GLD", 2, "etf_threshold", "GLD", theme="precious"),
            100,
            99,
            101,
            (1.0, 0, -2),
            99,
            99,
            99,
            0,
            "etf_threshold",
        ),
        Candidate(
            AttackPlan("SLV", 1, "etf_threshold", "SLV", theme="precious"),
            100,
            99,
            101,
            (1.1, 0, -1),
            99,
            99,
            99,
            0,
            "etf_threshold",
        ),
    ]

    assert [candidate.plan.symbol for candidate in select_one_per_theme(candidates)] == ["SLV"]


def test_observed_trade_minute_bars_builds_real_missing_minute_without_filling() -> None:
    rows = (
        (
            "DOO",
            {"t": "2026-08-19T13:36:05Z", "p": 67.0, "s": 10},
        ),
        (
            "DOO",
            {"t": "2026-08-19T13:36:45Z", "p": 67.2, "s": 20},
        ),
        (
            "DOO",
            {"t": "2026-08-19T13:37:10Z", "p": 67.1, "s": 5},
        ),
    )

    bars = observed_trade_minute_bars(rows)

    assert bars.to_dicts() == [
        {
            "symbol": "DOO",
            "ts_utc": datetime(2026, 8, 19, 13, 36, tzinfo=UTC),
            "open": 67.0,
            "high": 67.2,
            "low": 67.0,
            "close": 67.2,
            "volume": 30.0,
            "vwap": 67.13333333333334,
            "source": "alpaca.sip.rest.trades.observed_1m",
        },
        {
            "symbol": "DOO",
            "ts_utc": datetime(2026, 8, 19, 13, 37, tzinfo=UTC),
            "open": 67.1,
            "high": 67.1,
            "low": 67.1,
            "close": 67.1,
            "volume": 5.0,
            "vwap": 67.1,
            "source": "alpaca.sip.rest.trades.observed_1m",
        },
    ]


def test_rolling_box_uses_thirty_completed_minutes_before_current_minute() -> None:
    start = datetime(2026, 8, 19, 14, 37, tzinfo=UTC)
    frame = pl.DataFrame(
        [
            {
                "ts_utc": start + timedelta(minutes=index),
                "high": 100.0 + index,
                "low": 99.0 + index,
                "close": 99.5 + index,
                "volume": 100.0,
                "source": "sip",
            }
            for index in range(30)
        ]
    )

    box = rolling_box(frame, now_utc=datetime(2026, 8, 19, 15, 7, 30, tzinfo=UTC))

    assert box is not None
    assert box["start_utc"] == "2026-08-19T14:37:00+00:00"
    assert box["end_utc"] == "2026-08-19T15:07:00+00:00"
    assert box["high"] == 129.0
    assert box["low"] == 99.0


def test_clean_breakout_requires_complete_close_and_2_5x_volume() -> None:
    bars = [{"high": 101.2, "low": 100.1, "close": 101.0, "volume": 2_600.0}]

    assert clean_breakout_ready(bars, h30=100.0, vwap=99.8, median_volume=1_000.0) == (
        100.0,
        101.0,
        2.6,
    )
    assert (
        clean_breakout_ready(
            bars,
            h30=100.0,
            vwap=99.8,
            median_volume=1_000.0,
            minimum_volume_ratio=3.0,
        )
        is None
    )


def test_event_entry_requires_half_volume_pullback_holding_support() -> None:
    bars = [
        {"high": 101.2, "low": 100.1, "close": 101.0, "volume": 2_600.0},
        {"high": 101.0, "low": 100.0, "close": 100.4, "volume": 700.0},
    ]

    result = event_pullback_ready(bars, h30=100.0, vwap=99.8, median_volume=1_000.0)

    assert result == (100.0, 100.4, 2.6)


def test_b_level_requires_reclaim_after_half_volume_pullback() -> None:
    bars = [
        {"high": 101.2, "low": 100.1, "close": 101.0, "volume": 2_600.0},
        {"high": 101.0, "low": 100.0, "close": 100.4, "volume": 700.0},
        {"high": 101.4, "low": 100.3, "close": 101.2, "volume": 900.0},
    ]

    assert confirmed_retest_ready(bars, h30=100.0, vwap=99.8, median_volume=1_000.0) == (
        100.0,
        101.2,
        2.6,
    )


def test_exls_proxy_requires_three_times_h30_breakout_volume() -> None:
    bars = [
        {"high": 101.2, "low": 100.1, "close": 101.0, "volume": 2_900.0},
        {"high": 101.0, "low": 100.0, "close": 100.4, "volume": 700.0},
        {"high": 101.4, "low": 100.3, "close": 101.2, "volume": 900.0},
    ]

    assert (
        confirmed_retest_ready(
            bars,
            h30=100.0,
            vwap=99.8,
            median_volume=1_000.0,
            minimum_volume_ratio=3.0,
        )
        is None
    )


def test_box_must_fit_half_atr_and_six_percent_abort() -> None:
    assert box_within_atr(h30=102.0, l30=100.0, atr=4.0) is True
    assert box_within_atr(h30=102.1, l30=100.0, atr=4.0) is False
    assert box_within_atr(h30=107.0, l30=100.0, atr=20.0) is False


def test_attack_and_backup_plans_are_entry_eligible() -> None:
    assert AttackPlan("UGI", 1, "clean", "XLU").entry_eligible is True
    assert AttackPlan("HAE", 3, "confirm", "XLV").entry_eligible is True
    assert AttackPlan("AMLX", 3, "event", "XBI").entry_eligible is True
    assert AttackPlan("EXLS", 3, "rvol3", "IGV").entry_eligible is True
    assert AttackPlan("MRK", 3, "confirm", "XLV").entry_eligible is True


def test_today_active_pool_matches_execution_plan() -> None:
    assert ACTIVE_SYMBOLS == (
        "FCX",
        "HOOD",
        "AVGO",
        "LITE",
        "CF",
        "ROST",
        "NEM",
        "DE",
        "MSTR",
        "LUNR",
    )
