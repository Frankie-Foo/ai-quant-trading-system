from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from kernel.backtest import backtest_orb_trade, round_trip_costs
from kernel.config import load_config
from kernel.exits import make_exits
from kernel.labels import triple_barrier
from kernel.signals import orb5, orb5_intent
from kernel.sizing import size_position

OPEN = datetime(2026, 7, 20, 13, 30, tzinfo=UTC)


def _bars(values: list[tuple[float, float, float, float, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["FAST"] * len(values),
            "ts_utc": [OPEN + timedelta(minutes=i) for i in range(len(values))],
            "open": [row[0] for row in values],
            "high": [row[1] for row in values],
            "low": [row[2] for row in values],
            "close": [row[3] for row in values],
            "vwap": [row[4] for row in values],
            "volume": [1_000] * len(values),
        }
    ).with_columns(pl.col("ts_utc").cast(pl.Datetime("ms", "UTC")))


def test_orb5_requires_bullish_opening_range_and_enters_next_bar_vwap() -> None:
    bars = _bars(
        [
            (10.0, 10.2, 9.9, 10.1, 10.05),
            (10.1, 10.3, 10.0, 10.2, 10.15),
            (10.2, 10.4, 10.1, 10.3, 10.25),
            (10.3, 10.35, 10.2, 10.25, 10.28),
            (10.25, 10.45, 10.2, 10.4, 10.33),
            (10.4, 10.6, 10.35, 10.55, 10.50),  # breakout trigger
            (10.55, 10.7, 10.5, 10.65, 10.61),  # next-bar fill
        ]
    )
    signal = orb5(
        bars,
        session_open_utc=OPEN,
        asof_utc=OPEN + timedelta(minutes=7),
        rvol=4.0,
        min_rvol=3.0,
    )
    assert signal.triggered is True
    assert signal.trigger_ts_utc == OPEN + timedelta(minutes=5)
    assert signal.entry_ts_utc == OPEN + timedelta(minutes=6)
    assert signal.entry_px == pytest.approx(10.61)
    assert signal.opening_range_high == pytest.approx(10.45)


def test_orb5_does_not_use_unfinished_next_bar_or_bearish_range() -> None:
    bars = _bars(
        [
            (10.0, 10.2, 9.9, 10.1, 10.05),
            (10.1, 10.3, 10.0, 10.2, 10.15),
            (10.2, 10.4, 10.1, 10.3, 10.25),
            (10.3, 10.35, 10.2, 10.25, 10.28),
            (10.25, 10.45, 10.2, 10.4, 10.33),
            (10.4, 10.6, 10.35, 10.55, 10.50),
            (10.55, 10.7, 10.5, 10.65, 10.61),
        ]
    )
    pending = orb5(
        bars,
        session_open_utc=OPEN,
        asof_utc=OPEN + timedelta(minutes=6),
        rvol=4.0,
        min_rvol=3.0,
    )
    bearish = orb5(
        bars.with_columns(
            pl.when(pl.col("ts_utc") == OPEN + timedelta(minutes=4))
            .then(pl.lit(9.95))
            .otherwise(pl.col("close"))
            .alias("close")
        ),
        session_open_utc=OPEN,
        asof_utc=OPEN + timedelta(minutes=7),
        rvol=4.0,
        min_rvol=3.0,
    )
    assert pending.triggered is False
    assert pending.reason == "next_bar_unavailable_at_asof"
    assert bearish.triggered is False
    assert bearish.reason == "opening_range_not_bullish"


def test_orb5_fails_closed_when_opening_minute_is_missing() -> None:
    bars = _bars(
        [
            (10.0, 10.2, 9.9, 10.1, 10.05),
            (10.1, 10.3, 10.0, 10.2, 10.15),
            (10.2, 10.4, 10.1, 10.3, 10.25),
            (10.3, 10.35, 10.2, 10.25, 10.28),
            (10.25, 10.45, 10.2, 10.4, 10.33),
            (10.4, 10.6, 10.35, 10.55, 10.50),
            (10.55, 10.7, 10.5, 10.65, 10.61),
        ]
    ).filter(pl.col("ts_utc") != OPEN + timedelta(minutes=2))
    signal = orb5(
        bars,
        session_open_utc=OPEN,
        asof_utc=OPEN + timedelta(minutes=7),
        rvol=4.0,
        min_rvol=3.0,
    )
    assert signal.triggered is False
    assert signal.reason == "opening_range_missing_bars"


def test_orb5_does_not_replace_missing_next_minute_with_later_bar() -> None:
    bars = _bars(
        [
            (10.0, 10.2, 9.9, 10.1, 10.05),
            (10.1, 10.3, 10.0, 10.2, 10.15),
            (10.2, 10.4, 10.1, 10.3, 10.25),
            (10.3, 10.35, 10.2, 10.25, 10.28),
            (10.25, 10.45, 10.2, 10.4, 10.33),
            (10.4, 10.6, 10.35, 10.55, 10.50),
            (10.55, 10.7, 10.5, 10.65, 10.61),
            (10.65, 10.8, 10.6, 10.75, 10.71),
        ]
    ).filter(pl.col("ts_utc") != OPEN + timedelta(minutes=6))
    signal = orb5(
        bars,
        session_open_utc=OPEN,
        asof_utc=OPEN + timedelta(minutes=8),
        rvol=4.0,
        min_rvol=3.0,
    )
    assert signal.triggered is False
    assert signal.reason == "next_minute_bar_missing"


def test_live_orb5_intent_triggers_at_bar_boundary_without_future_fill_bar() -> None:
    bars = _bars(
        [
            (10.0, 10.2, 9.9, 10.1, 10.05),
            (10.1, 10.3, 10.0, 10.2, 10.15),
            (10.2, 10.4, 10.1, 10.3, 10.25),
            (10.3, 10.35, 10.2, 10.25, 10.28),
            (10.25, 10.45, 10.2, 10.4, 10.33),
            (10.4, 10.6, 10.35, 10.55, 10.50),
        ]
    )

    intent = orb5_intent(
        bars,
        session_open_utc=OPEN,
        decision_at_utc=OPEN + timedelta(minutes=6),
        rvol=4.0,
        min_rvol=3.0,
    )

    assert intent.triggered is True
    assert intent.trigger_ts_utc == OPEN + timedelta(minutes=5)
    assert intent.planned_entry_ts_utc == OPEN + timedelta(minutes=6)
    assert intent.provenance.endswith("entry=submit_at_next_bar_boundary")


def test_live_orb5_intent_refuses_late_replay_or_missing_minute() -> None:
    bars = _bars(
        [
            (10.0, 10.2, 9.9, 10.1, 10.05),
            (10.1, 10.3, 10.0, 10.2, 10.15),
            (10.2, 10.4, 10.1, 10.3, 10.25),
            (10.3, 10.35, 10.2, 10.25, 10.28),
            (10.25, 10.45, 10.2, 10.4, 10.33),
            (10.4, 10.6, 10.35, 10.55, 10.50),
            (10.55, 10.7, 10.5, 10.65, 10.61),
        ]
    )
    late = orb5_intent(
        bars,
        session_open_utc=OPEN,
        decision_at_utc=OPEN + timedelta(minutes=7),
        rvol=4.0,
        min_rvol=3.0,
    )
    missing = orb5_intent(
        bars.filter(pl.col("ts_utc") != OPEN + timedelta(minutes=2)),
        session_open_utc=OPEN,
        decision_at_utc=OPEN + timedelta(minutes=7),
        rvol=4.0,
        min_rvol=3.0,
    )

    assert late.triggered is False
    assert late.reason == "first_breakout_is_stale"
    assert missing.triggered is False
    assert missing.reason == "minute_path_incomplete"


@pytest.mark.parametrize(
    ("future", "expected"),
    [
        ([(10.0, 12.1, 9.8, 11.5, 11.0)], "tp"),
        ([(10.0, 10.2, 8.9, 9.2, 9.5)], "sl"),
        ([(10.0, 12.1, 8.9, 10.5, 10.2)], "sl"),
    ],
)
def test_triple_barrier_tp_sl_and_conservative_same_bar(
    future: list[tuple[float, float, float, float, float]], expected: str
) -> None:
    bars = _bars([(10.0, 10.1, 9.9, 10.0, 10.0)] + future)
    event = triple_barrier(
        bars,
        entry_ts=OPEN,
        entry_px=10.0,
        tp_px=12.0,
        sl_px=9.0,
        time_stop=OPEN + timedelta(minutes=10),
    )
    assert event.which == expected


def test_triple_barrier_time_exit_uses_last_known_bar_at_stop() -> None:
    bars = _bars(
        [
            (10.0, 10.1, 9.9, 10.0, 10.0),
            (10.0, 10.2, 9.8, 10.1, 10.05),
            (10.1, 10.3, 10.0, 10.2, 10.15),
        ]
    )
    event = triple_barrier(
        bars,
        entry_ts=OPEN,
        entry_px=10.0,
        tp_px=12.0,
        sl_px=9.0,
        time_stop=OPEN + timedelta(minutes=2),
    )
    assert event.which == "time"
    assert event.exit_ts == OPEN + timedelta(minutes=2)
    assert event.exit_px == pytest.approx(10.2)


def test_triple_barrier_stop_hit_on_time_stop_bar_wins_over_close() -> None:
    bars = _bars(
        [
            (10.0, 10.1, 9.9, 10.0, 10.0),
            (10.0, 10.2, 9.8, 10.1, 10.05),
            (10.1, 10.3, 8.9, 10.2, 10.10),
        ]
    )
    event = triple_barrier(
        bars,
        entry_ts=OPEN,
        entry_px=10.0,
        tp_px=12.0,
        sl_px=9.0,
        time_stop=OPEN + timedelta(minutes=2),
    )
    assert event.which == "sl"
    assert event.exit_px == pytest.approx(9.0)


def test_triple_barrier_rejects_missing_post_entry_minute() -> None:
    bars = _bars(
        [
            (10.0, 10.1, 9.9, 10.0, 10.0),
            (10.0, 10.2, 9.8, 10.1, 10.05),
            (10.1, 10.3, 10.0, 10.2, 10.15),
        ]
    ).filter(pl.col("ts_utc") != OPEN + timedelta(minutes=1))
    with pytest.raises(ValueError, match="missing post-entry minute bar"):
        triple_barrier(
            bars,
            entry_ts=OPEN,
            entry_px=10.0,
            tp_px=12.0,
            sl_px=9.0,
            time_stop=OPEN + timedelta(minutes=2),
        )


def test_exit_plan_uses_atr_and_half_day_time_stop() -> None:
    cfg = load_config("config.yaml")
    normal = make_exits(10.0, 0.5, trade_date=date(2026, 7, 20), is_half_day=False, cfg=cfg)
    half = make_exits(10.0, 0.5, trade_date=date(2025, 7, 3), is_half_day=True, cfg=cfg)
    assert normal.tp_px == pytest.approx(11.0)
    assert normal.sl_px == pytest.approx(9.5)
    assert normal.time_stop_utc == datetime(2026, 7, 20, 19, 55, tzinfo=UTC)
    assert half.time_stop_utc == datetime(2025, 7, 3, 16, 55, tzinfo=UTC)


def test_sizing_reproduces_frozen_600_share_example() -> None:
    cfg = load_config("config.yaml").model_copy(update={"risk_per_trade": 0.003})
    result = size_position(
        symbol="FAST",
        price=12.0,
        atr14=0.60,
        adv_usd=10_000_000.0,
        tier="mid",
        confidence=0.6,
        cfg=cfg,
    )
    assert result.risk_cap == pytest.approx(12_000.0)
    assert result.final_notional == pytest.approx(7_200.0)
    assert result.shares == 600
    assert result.binding_cap == "risk_cap"


def test_sizing_caps_capital_to_actual_paper_equity() -> None:
    cfg = load_config("config.yaml").model_copy(update={"risk_per_trade": 0.003})
    result = size_position(
        symbol="FAST",
        price=12.0,
        atr14=0.60,
        adv_usd=10_000_000.0,
        tier="mid",
        confidence=0.6,
        cfg=cfg,
        capital_override=100_000.0,
    )

    assert result.capital_base == pytest.approx(100_000.0)
    assert result.risk_cap == pytest.approx(6_000.0)
    assert result.final_notional == pytest.approx(3_600.0)
    assert result.shares == 300


def test_round_trip_costs_include_both_legs_and_stop_slippage() -> None:
    cfg = load_config("config.yaml")
    costs = round_trip_costs(
        shares=1_000,
        entry_px=12.0,
        exit_px=12.0,
        cs_spread=0.001,
        participation=0.04,
        atr_pct=0.05,
        atr=0.60,
        stopped=True,
        cfg=cfg,
    )
    assert costs.commission == pytest.approx(7.0)
    assert costs.sec_fee == pytest.approx(0.3336)
    assert costs.finra_taf == pytest.approx(0.166)
    assert costs.spread == pytest.approx(24.0)
    assert costs.impact == pytest.approx(24.0)
    assert costs.stop_slippage == pytest.approx(300.0)
    assert costs.total == pytest.approx(355.4996)


def test_end_to_end_orb_trade_uses_next_bar_and_reports_net_pnl() -> None:
    bars = _bars(
        [
            (10.0, 10.2, 9.9, 10.1, 10.05),
            (10.1, 10.3, 10.0, 10.2, 10.15),
            (10.2, 10.4, 10.1, 10.3, 10.25),
            (10.3, 10.35, 10.2, 10.25, 10.28),
            (10.25, 10.45, 10.2, 10.4, 10.33),
            (10.4, 10.6, 10.35, 10.55, 10.50),
            (10.55, 10.7, 10.5, 10.65, 10.61),
            (10.65, 11.8, 10.6, 11.7, 11.5),
            (11.7, 12.0, 11.6, 11.9, 11.8),
        ]
    ).with_columns(pl.lit(100_000).alias("volume"))
    trade = backtest_orb_trade(
        bars,
        symbol="FAST",
        trade_date=date(2026, 7, 20),
        session_open_utc=OPEN,
        session_close_utc=OPEN + timedelta(minutes=9),
        is_half_day=False,
        rvol=4.0,
        atr14=0.50,
        adv_usd=10_000_000,
        tier="mid",
        confidence=0.6,
        cs_spread=0.001,
        cfg=load_config("config.yaml"),
    )
    assert trade is not None
    assert trade.signal.entry_ts_utc == OPEN + timedelta(minutes=6)
    assert trade.barrier.which == "tp"
    assert trade.net_pnl < trade.gross_pnl
