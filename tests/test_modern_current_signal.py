from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl
import pytest

from research import modern_momentum as modern
from research.h30_challenger import _FiveMinuteBar

OPENED = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)


def bars(count: int = 90, *, breakout_at: int = 26) -> pl.DataFrame:
    rows = []
    for minute in range(count):
        close = 99.4 + min(minute, 14) * 0.035
        if 15 <= minute < breakout_at:
            close = 99.85
        if minute >= breakout_at:
            close = 100.45 + (minute - breakout_at) * 0.08
        rows.append(
            {
                "symbol": "TEST",
                "ts_utc": OPENED + timedelta(minutes=minute),
                "open": close - 0.02,
                "high": close + 0.08,
                "low": close - 0.08,
                "close": close,
                "vwap": close,
                "volume": 10_000,
            }
        )
    return pl.DataFrame(rows)


def latest(frame: pl.DataFrame, minute: int, **kwargs: Any) -> modern.ModernMomentumSignal | None:
    return modern.latest_modern_momentum_signal(
        frame,
        session_open_utc=OPENED,
        prior_close=96.0,
        market_cap=2e9,
        premarket_rvol=2.0,
        config=modern.ModernMomentumConfig(),
        asof_utc=OPENED + timedelta(minutes=minute),
        **kwargs,
    )


def test_latest_signal_needs_no_future_fill_bar_and_remains_one_minute() -> None:
    signal = latest(bars(28), 28)

    assert signal is not None
    assert signal.signal_ts_utc == OPENED + timedelta(minutes=28)
    assert signal.entry_reference == pytest.approx(100.53)
    assert signal.stop_level == pytest.approx(99.77)
    assert signal.h15 == pytest.approx(99.97)
    assert signal.macd > 0
    assert signal.premarket_rvol == 2.0
    assert 0.005 < signal.all_in_stop_pct <= 0.02


def test_latest_ignores_incomplete_future_bars_and_never_replays_an_old_trade() -> None:
    frame = bars()
    expected = latest(frame.head(40), 40)
    assert expected is not None
    assert expected.signal_ts_utc == OPENED + timedelta(minutes=40)
    assert latest(frame, 40) == expected
    assert latest(frame.head(39), 40) is None
    # A failed current signal must not return an earlier still-valid breakout.
    failed = frame.head(41).with_columns(
        pl.when(pl.col("ts_utc") == OPENED + timedelta(minutes=40))
        .then(99.0)
        .otherwise(pl.col("close"))
        .alias("close")
    )
    assert latest(failed, 41) is None


def replay(frame: pl.DataFrame, **kwargs: Any) -> modern.ModernMomentumTrade | None:
    return modern.evaluate_modern_momentum(
        frame,
        session_open_utc=OPENED,
        prior_close=96.0,
        market_cap=2e9,
        premarket_rvol=2.0,
        config=kwargs.pop("config", modern.ModernMomentumConfig()),
        **kwargs,
    )


def test_replay_reuses_signal_but_executes_only_the_immediate_next_bar() -> None:
    frame = bars(29)
    signal = latest(frame.head(28), 28)
    trade = replay(frame)
    assert signal is not None and trade is not None
    assert trade.signal_ts_utc == signal.signal_ts_utc
    assert trade.stop_level == signal.stop_level
    assert trade.entry_px == pytest.approx(100.660413)
    assert replay(frame.head(28)) is None
    missing_fill = bars(30).filter(pl.col("ts_utc") != OPENED + timedelta(minutes=28))
    assert replay(missing_fill) is None


def test_replay_cannot_use_a_future_cheap_fill_to_rescue_ineligible_signal_risk() -> None:
    frame = bars(29).with_columns(
        pl.when(pl.col("ts_utc") == OPENED + timedelta(minutes=27))
        .then(105.0)
        .otherwise(pl.col("close"))
        .alias("close"),
        pl.when(pl.col("ts_utc") == OPENED + timedelta(minutes=27))
        .then(105.1)
        .otherwise(pl.col("high"))
        .alias("high"),
    )
    assert latest(frame.head(28), 28) is None
    assert replay(frame) is None


@pytest.mark.parametrize("breakout_at,allowed", [(327, True), (328, False), (329, False)])
def test_default_first_entry_cutoff_is_strictly_before_1500(
    breakout_at: int,
    allowed: bool,
) -> None:
    # After the flat segment MACD needs two rising samples: signal at breakout + 2.
    frame = bars(breakout_at + 3, breakout_at=breakout_at)
    assert (latest(frame.head(breakout_at + 2), breakout_at + 2) is not None) is allowed
    assert (replay(frame) is not None) is allowed


def test_first_entry_default_liquidation_labels_use_1550() -> None:
    frame = bars(390).with_columns(
        pl.when(pl.col("ts_utc") >= OPENED + timedelta(minutes=29))
        .then(100.59)
        .otherwise(pl.col("open"))
        .alias("open"),
        pl.when(pl.col("ts_utc") >= OPENED + timedelta(minutes=29))
        .then(100.65)
        .otherwise(pl.col("high"))
        .alias("high"),
        pl.when(pl.col("ts_utc") >= OPENED + timedelta(minutes=29))
        .then(100.5)
        .otherwise(pl.col("low"))
        .alias("low"),
        pl.when(pl.col("ts_utc") >= OPENED + timedelta(minutes=29))
        .then(100.6)
        .otherwise(pl.col("close"))
        .alias("close"),
    )
    trade = replay(frame, config=replace(modern.ModernMomentumConfig(), target_r=100.0))
    assert trade is not None
    assert trade.exit_reason == "time_exit"
    assert trade.exit_ts_utc == OPENED + timedelta(minutes=380)


def test_manifest_hashes_effective_modern_config_not_legacy_policy() -> None:
    manifest = modern.modern_strategy_manifest()
    encoded = json.dumps(manifest["effective_config"], sort_keys=True, separators=(",", ":"))
    assert manifest["schema_version"] == "modern_strategy_manifest.v1"
    assert manifest["strategy_version"] == "modern-h15-current-signal.v4"
    assert manifest["effective_config"]["minimum_premarket_rvol"] == 1.5
    assert manifest["effective_config"]["maximum_entry_relative_spread"] == 0.0025
    assert manifest["effective_config"]["signal_cutoff_minutes"] == 330
    assert manifest["effective_config"]["liquidation_minutes"] == 380
    assert manifest["first_entry_bar_minutes"] == 1
    assert manifest["reentry_bar_minutes"] == 5
    assert manifest["config_sha256"] == hashlib.sha256(encoded.encode()).hexdigest()
    json.dumps(manifest, allow_nan=False)
    custom = modern.modern_strategy_manifest(
        replace(modern.ModernMomentumConfig(), minimum_premarket_rvol=2.0)
    )
    assert custom["effective_config"]["minimum_premarket_rvol"] == 2.0
    assert custom["config_sha256"] != manifest["config_sha256"]


def exit_reason(frame: pl.DataFrame, minute: int, **kwargs: Any) -> str | None:
    return modern.actual_position_exit_reason(
        frame,
        session_open_utc=OPENED,
        entered_at_utc=kwargs.pop("entered_at_utc", OPENED + timedelta(minutes=28)),
        asof_utc=OPENED + timedelta(minutes=minute),
        target_level=kwargs.pop("target_level", 200.0),
        liquidation_utc=OPENED + timedelta(minutes=380),
        attempt=kwargs.pop("attempt", 1),
        **kwargs,
    )


def test_actual_first_position_exit_uses_current_completed_vwap_macd_not_a_replay() -> None:
    frame = bars(42).with_columns(
        pl.when(pl.col("ts_utc") >= OPENED + timedelta(minutes=38))
        .then(98.0)
        .otherwise(pl.col("close"))
        .alias("close"),
    )
    assert exit_reason(frame, 40) == "trend_exit"
    assert exit_reason(frame.head(40), 40) == "trend_exit"
    assert exit_reason(frame.head(40), 41) is None
    # The replay waits for the second held minute before a first-attempt trend exit.
    assert exit_reason(frame, 40, entered_at_utc=OPENED + timedelta(minutes=39)) is None
    assert exit_reason(frame, 40, entered_at_utc=OPENED + timedelta(minutes=38)) == "trend_exit"


def test_actual_exit_ignores_historical_targets_and_broker_stop_crossings() -> None:
    historical_target = bars(40).with_columns(
        pl.when(pl.col("ts_utc") == OPENED + timedelta(minutes=29))
        .then(300.0)
        .otherwise(pl.col("high"))
        .alias("high"),
        pl.when(pl.col("ts_utc") == OPENED + timedelta(minutes=39))
        .then(1.0)
        .otherwise(pl.col("low"))
        .alias("low"),
    )
    assert exit_reason(historical_target, 40) is None
    assert exit_reason(bars(40), 40, target_level=100.0) == "target_3r"
    assert exit_reason(bars(40), 380, target_level=100.0) == "time_exit"


def test_actual_second_position_exit_requires_a_new_completed_five_minute_trend() -> None:
    frame = (
        bars(41)
        .with_columns(
            pl.when(pl.col("ts_utc") >= OPENED + timedelta(minutes=35))
            .then(97.0)
            .when(pl.col("ts_utc") >= OPENED + timedelta(minutes=30))
            .then(98.0)
            .otherwise(pl.col("close"))
            .alias("close"),
        )
        .with_columns(
            (pl.col("close") + 0.08).alias("high"),
            (pl.col("close") - 0.08).alias("low"),
            pl.col("close").alias("vwap"),
        )
    )
    assert exit_reason(frame, 39, attempt=2) is None
    assert exit_reason(frame, 40, attempt=2) == "trend_exit"
    assert exit_reason(frame, 41, attempt=2) is None
    assert exit_reason(frame.head(39), 40, attempt=2) is None


def test_latest_signal_does_not_return_the_days_already_exited_first_trade() -> None:
    frame = bars()
    old_trade = replay(frame)
    assert old_trade is not None and old_trade.exit_ts_utc < OPENED + timedelta(minutes=80)
    signal = latest(frame, 80)
    assert signal is not None
    assert signal.signal_ts_utc == OPENED + timedelta(minutes=80)


@pytest.mark.parametrize("spread,allowed", [(0.0025, True), (0.0026, False)])
def test_current_signal_and_replay_share_the_spread_limit(spread: float, allowed: bool) -> None:
    frame = bars(29)
    assert (latest(frame, 28, relative_spread=spread) is not None) is allowed
    assert (replay(frame, relative_spread=spread) is not None) is allowed


@pytest.mark.parametrize("spread", [-0.001, float("nan"), float("inf")])
def test_current_signal_rejects_invalid_spread(spread: float) -> None:
    with pytest.raises(ValueError, match="relative_spread"):
        latest(bars(), 28, relative_spread=spread)


@pytest.mark.parametrize(
    "field,value",
    [
        ("prior_close", 100.0),
        ("prior_close", float("nan")),
        ("market_cap", 999_999_999.0),
        ("market_cap", float("nan")),
        ("premarket_rvol", 1.49),
        ("premarket_rvol", float("nan")),
    ],
)
def test_current_signal_preserves_market_cap_gap_and_rvol_gates(field: str, value: float) -> None:
    inputs = {"prior_close": 96.0, "market_cap": 2e9, "premarket_rvol": 2.0, field: value}
    assert (
        modern.latest_modern_momentum_signal(
            bars(28),
            session_open_utc=OPENED,
            asof_utc=OPENED + timedelta(minutes=28),
            config=modern.ModernMomentumConfig(),
            **inputs,
        )
        is None
    )


def test_current_signal_preserves_h15_volume_and_ignores_future_invalid_data() -> None:
    frame = bars(29).with_columns(
        pl.when(pl.col("ts_utc") >= OPENED + timedelta(minutes=28))
        .then(float("nan"))
        .otherwise(pl.col("close"))
        .alias("close"),
    )
    assert latest(frame, 28) is not None
    assert latest(frame.with_columns(pl.lit(1).alias("volume")), 28) is None


@pytest.mark.parametrize(
    "end_minute,spread,allowed",
    [
        (325, 0.0025, True),
        (325, 0.0026, False),
        (330, 0.001, False),
        (335, 0.001, False),
    ],
)
def test_current_reentry_shares_default_cutoff_spread_and_stale_guards(
    end_minute: int,
    spread: float,
    allowed: bool,
) -> None:
    fives = [
        _FiveMinuteBar(
            OPENED + timedelta(minutes=end_minute - 15),
            30.7,
            31.0,
            30.4,
            30.7,
            100_000,
            30.7,
            30.2,
            30.7,
            30.7,
        ),
        _FiveMinuteBar(
            OPENED + timedelta(minutes=end_minute - 10),
            31.2,
            31.24,
            30.95,
            31.2,
            75_000,
            31.2,
            30.3,
            31.2,
            31.2,
        ),
        _FiveMinuteBar(
            OPENED + timedelta(minutes=end_minute - 5),
            31.52,
            31.56,
            31.14,
            31.52,
            90_000,
            31.52,
            30.4,
            31.52,
            31.52,
        ),
    ]
    kwargs: dict[str, Any] = {
        "stopped_at_utc": OPENED + timedelta(minutes=end_minute - 16),
        "h15": 30.69,
        "relative_spread": spread,
    }
    assert (
        modern.pullback_reentry(
            fives,
            asof_utc=OPENED + timedelta(minutes=end_minute),
            **kwargs,
        )
        is not None
    ) is allowed
    assert (
        modern.pullback_reentry(
            fives,
            asof_utc=OPENED + timedelta(minutes=end_minute - 1),
            **kwargs,
        )
        is None
    )
    assert (
        modern.pullback_reentry(
            fives,
            asof_utc=OPENED + timedelta(minutes=end_minute + 5),
            **kwargs,
        )
        is None
    )
    assert (
        modern.pullback_reentry(
            fives,
            asof_utc=OPENED + timedelta(minutes=end_minute),
            config=replace(modern.ModernMomentumConfig(), max_all_in_stop_pct=0.019),
            **kwargs,
        )
        is None
    )
