from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl

from kernel.technical_monitor import (
    AggregatedBar,
    LongGreenExpansion,
    PositionPlan,
    QuoteSnapshot,
    TimeframeSnapshot,
    build_long_green_expansion,
    build_trade_advisory,
    resample_completed_bars,
)


def _schedule() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "trade_date": [date(2026, 7, 24)],
            "market_open_utc": [datetime(2026, 7, 24, 13, 30, tzinfo=UTC)],
            "market_close_utc": [datetime(2026, 7, 24, 20, 0, tzinfo=UTC)],
        }
    )


def _bars(count: int = 40) -> pl.DataFrame:
    start = datetime(2026, 7, 24, 13, 30, tzinfo=UTC)
    rows: list[dict[str, object]] = []
    for index in range(count):
        close = 46.0 + (index * 0.02)
        rows.append(
            {
                "symbol": "RNG",
                "ts_utc": start + timedelta(minutes=index),
                "open": close - 0.01,
                "high": close + 0.03,
                "low": close - 0.03,
                "close": close,
                "volume": 1_000 + index,
                "trade_count": 10,
                "vwap": close,
            }
        )
    return pl.DataFrame(rows)


def _snapshot(
    timeframe: str,
    *,
    close: float = 47.50,
    boll_mid: float | None = 47.00,
    boll_upper: float | None = 48.00,
    macd_hist: float | None = 0.10,
    kdj_k: float | None = 70.0,
    kdj_d: float | None = 60.0,
    green_volume_ratio: float | None = 1.8,
) -> TimeframeSnapshot:
    return TimeframeSnapshot(
        timeframe=timeframe,
        completed_at_utc=datetime(2026, 7, 24, 14, 45, tzinfo=UTC),
        close=close,
        boll_mid=boll_mid,
        boll_upper=boll_upper,
        boll_lower=46.00,
        macd_dif=0.20,
        macd_dea=0.10,
        macd_hist=macd_hist,
        kdj_k=kdj_k,
        kdj_d=kdj_d,
        kdj_j=90.0,
        last_confirmed_top=47.40,
        last_confirmed_bottom=46.90,
        prior_confirmed_bottom=46.70,
        green_volume_ratio=green_volume_ratio,
        bar_count=100,
    )


def _expansion(*, qualified: bool = True) -> LongGreenExpansion:
    return LongGreenExpansion(
        qualified=qualified,
        completed_at_utc=datetime(2026, 7, 24, 14, 45, tzinfo=UTC),
        elapsed_minutes=75,
        session_open=40.50,
        session_high=48.00,
        session_low=40.30,
        session_close=47.50,
        session_return=0.17284,
        body_to_range=0.909091,
        close_location=0.935065,
        session_vwap=45.00,
        close_vs_vwap=0.055556,
        cumulative_volume=10_000_000,
        max_green_volume_ratio=2.5,
        premarket_rvol=32.0,
        score=96.0,
        blockers=() if qualified else ("session_return_below_min",),
    )


def _session_bars() -> tuple[AggregatedBar, ...]:
    start = datetime(2026, 7, 24, 13, 30, tzinfo=UTC)
    result: list[AggregatedBar] = []
    for index in range(30):
        open_price = 40.0 + (index * 0.1)
        close = open_price + 0.08
        volume = 1_000
        if index == 25:
            close = open_price + 0.45
            volume = 2_000
        result.append(
            AggregatedBar(
                ts_utc=start + timedelta(minutes=index),
                completed_at_utc=start + timedelta(minutes=index + 1),
                trade_date=date(2026, 7, 24),
                open=open_price,
                high=close + 0.03,
                low=open_price - 0.02,
                close=close,
                volume=volume,
                trade_count=20,
                vwap=(open_price + close) / 2,
                source_bar_count=1,
            )
        )
    return tuple(result)


def test_resample_excludes_incomplete_bucket_without_lookahead() -> None:
    as_of = datetime(2026, 7, 24, 13, 37, 30, tzinfo=UTC)

    bars = resample_completed_bars(
        _bars(),
        _schedule(),
        interval_minutes=5,
        as_of_utc=as_of,
    )

    assert len(bars) == 1
    assert bars[0].ts_utc == datetime(2026, 7, 24, 13, 30, tzinfo=UTC)
    assert bars[0].completed_at_utc == datetime(2026, 7, 24, 13, 35, tzinfo=UTC)
    assert bars[0].source_bar_count == 5


def test_long_green_expansion_requires_body_location_vwap_and_green_volume() -> None:
    profile = build_long_green_expansion(
        _session_bars(),
        trade_date=date(2026, 7, 24),
    )

    assert profile is not None
    assert profile.qualified is True
    assert profile.session_return > 0.04
    assert profile.body_to_range > 0.60
    assert profile.close_location > 0.80
    assert profile.max_green_volume_ratio == 2.0
    assert profile.score >= 70


def test_big_green_price_move_without_volume_confirmation_is_rejected() -> None:
    bars = tuple(
        AggregatedBar(
            **{
                **bar.__dict__,
                "volume": 1_000,
            }
        )
        for bar in _session_bars()
    )

    profile = build_long_green_expansion(
        bars,
        trade_date=date(2026, 7, 24),
    )

    assert profile is not None
    assert profile.qualified is False
    assert "green_volume_expansion_below_min" in profile.blockers


def test_premarket_rvol_can_confirm_broad_volume_when_every_opening_bar_is_active() -> None:
    bars = tuple(
        AggregatedBar(
            **{
                **bar.__dict__,
                "volume": 1_000,
            }
        )
        for bar in _session_bars()
    )

    profile = build_long_green_expansion(
        bars,
        trade_date=date(2026, 7, 24),
        premarket_rvol=32.0,
    )

    assert profile is not None
    assert profile.qualified is True
    assert profile.premarket_rvol == 32.0


def test_buy_requires_realtime_fresh_tight_quote_and_all_timeframe_confirmation() -> None:
    quote = QuoteSnapshot(
        observed_at_utc=datetime(2026, 7, 24, 14, 45, tzinfo=UTC),
        bid=47.49,
        ask=47.51,
        age_seconds=1.0,
        feed="sip",
        is_realtime=True,
    )
    plan = PositionPlan(
        position_shares=45,
        position_average=46.50,
        new_lot_shares=25,
        new_lot_entry=46.70,
        new_lot_protect=47.20,
        all_exit=46.20,
        add_shares=25,
    )

    advisory = build_trade_advisory(
        quote=quote,
        one_minute=_snapshot("1m"),
        five_minute=_snapshot("5m"),
        fifteen_minute=_snapshot("15m"),
        long_green_expansion=_expansion(),
        session_vwap=47.00,
        market_is_open=True,
        plan=plan,
    )

    assert advisory.action == "BUY_ADD"
    assert advisory.shares == 25
    assert advisory.order_authorized is False


def test_buy_fails_closed_when_spread_is_wide_or_volume_is_not_confirmed() -> None:
    quote = QuoteSnapshot(
        observed_at_utc=datetime(2026, 7, 24, 14, 45, tzinfo=UTC),
        bid=47.30,
        ask=47.70,
        age_seconds=1.0,
        feed="sip",
        is_realtime=True,
    )
    plan = PositionPlan(
        position_shares=45,
        position_average=46.50,
        new_lot_shares=25,
        new_lot_entry=46.70,
        new_lot_protect=47.20,
        all_exit=46.20,
        add_shares=25,
    )

    advisory = build_trade_advisory(
        quote=quote,
        one_minute=_snapshot("1m", green_volume_ratio=1.1),
        five_minute=_snapshot("5m"),
        fifteen_minute=_snapshot("15m"),
        long_green_expansion=_expansion(qualified=False),
        session_vwap=47.00,
        market_is_open=True,
        plan=plan,
    )

    assert advisory.action == "HOLD"
    assert "spread_too_wide" in advisory.blockers
    assert "no_confirmed_green_volume" in advisory.blockers
    assert "long_green_expansion_not_confirmed" in advisory.blockers


def test_hard_protection_precedes_bullish_indicators() -> None:
    quote = QuoteSnapshot(
        observed_at_utc=datetime(2026, 7, 24, 14, 45, tzinfo=UTC),
        bid=46.08,
        ask=46.10,
        age_seconds=1.0,
        feed="sip",
        is_realtime=True,
    )
    plan = PositionPlan(
        position_shares=45,
        position_average=46.50,
        new_lot_shares=25,
        new_lot_entry=46.70,
        new_lot_protect=47.20,
        all_exit=46.20,
        add_shares=25,
    )

    advisory = build_trade_advisory(
        quote=quote,
        one_minute=_snapshot("1m"),
        five_minute=_snapshot("5m"),
        fifteen_minute=_snapshot("15m"),
        long_green_expansion=_expansion(),
        session_vwap=45.90,
        market_is_open=True,
        plan=plan,
    )

    assert advisory.action == "EXIT_ALL"
    assert advisory.shares == 45
    assert advisory.order_authorized is False
