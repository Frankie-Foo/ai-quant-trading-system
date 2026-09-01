from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from research.h30_challenger import (
    H30Config,
    assess_h30_challenger,
    evaluate_h30_path,
    sector_proxy_from_sic,
)

OPEN = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)


def _trend_bars() -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    price = 100.0
    for minute in range(90):
        bucket = minute // 5
        if bucket < 6:
            close = 100.0 + bucket * 0.12
            volume = 1_000
        elif bucket == 6:  # 10:00 breakout
            close = 101.35
            volume = 2_000
        elif bucket == 7:  # reduced-volume retest holding H30
            close = 100.90
            volume = 700
        elif bucket == 8:  # reclaim above pullback high
            close = 101.60
            volume = 1_800
        else:
            close = 101.60 + (bucket - 8) * 0.15
            volume = 1_200
        open_px = price
        high = max(open_px, close) + 0.08
        low = min(open_px, close) - 0.05
        rows.append(
            {
                "symbol": "TEST",
                "ts_utc": OPEN + timedelta(minutes=minute),
                "open": open_px,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "vwap": (open_px + close) / 2,
            }
        )
        price = close
    return pl.DataFrame(rows).with_columns(
        pl.col("ts_utc").cast(pl.Datetime("ms", "UTC"))
    )


def _continuation_bars() -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    price = 100.0
    for minute in range(60):
        bucket = minute // 5
        if bucket < 6:
            close = 100.0 + bucket * 0.1
            volume = 1_000
        else:
            close = 102.0 + (bucket - 6) * 0.5
            volume = 1_500
        rows.append(
            {
                "symbol": "FAST",
                "ts_utc": OPEN + timedelta(minutes=minute),
                "open": price,
                "high": max(price, close) + 0.05,
                "low": min(price, close) - 0.03,
                "close": close,
                "volume": volume,
                "vwap": (price + close) / 2,
            }
        )
        price = close
    return pl.DataFrame(rows).with_columns(
        pl.col("ts_utc").cast(pl.Datetime("ms", "UTC"))
    )


def test_h30_challenger_waits_for_breakout_retest_and_reclaim() -> None:
    result = evaluate_h30_path(_trend_bars(), session_open_utc=OPEN)

    assert result.status == "traded"
    assert result.branch == "narrow"
    assert result.entry_ts_utc == OPEN + timedelta(minutes=45)
    assert result.entry_px is not None
    assert result.entry_px > 101.5
    assert result.ema_score >= 3
    assert result.risk_fraction in {0.75, 1.0}


def test_h30_challenger_fails_closed_on_missing_h30_minute() -> None:
    bars = _trend_bars().filter(pl.col("ts_utc") != OPEN + timedelta(minutes=12))

    result = evaluate_h30_path(bars, session_open_utc=OPEN)

    assert result.status == "blocked"
    assert result.reason == "h30_incomplete"


def test_h30_stop_and_slippage_never_exceed_two_percent() -> None:
    cfg = H30Config()

    assert cfg.price_stop_pct + cfg.stop_slippage_reserve_pct == 0.02


def test_h30_challenger_rejects_small_cost_incomplete_sample() -> None:
    decision = assess_h30_challenger(
        baseline_net_pnl=-6_400,
        challenger_net_pnl=-7_400,
        challenger_profit_factor=0.81,
        baseline_max_drawdown=-14_100,
        challenger_max_drawdown=-25_700,
        trade_legs=12,
        fold_wins=2,
        nbbo_cost_complete=False,
    )

    assert decision.status == "rejected"
    assert decision.production_eligible is False
    assert "historical_nbbo_costs_missing" in decision.reasons


def test_sic_mapping_uses_frozen_liquid_proxies() -> None:
    assert sector_proxy_from_sic("3674") == "SMH"
    assert sector_proxy_from_sic("1311") == "XLE"
    assert sector_proxy_from_sic("4841") == "XLC"
    assert sector_proxy_from_sic("not-known") is None


def test_trend_continuation_is_independent_and_time_bounded() -> None:
    baseline = evaluate_h30_path(_continuation_bars(), session_open_utc=OPEN)
    challenger = evaluate_h30_path(
        _continuation_bars(),
        session_open_utc=OPEN,
        cfg=H30Config(allow_trend_continuation=True, entry_cutoff_minutes=150),
    )

    assert baseline.status == "no_trade"
    assert challenger.status == "traded"
    assert challenger.entry_route == "trend_continuation"
    assert challenger.entry_ts_utc is not None
    assert challenger.entry_ts_utc < OPEN + timedelta(minutes=150)
