from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from research.pullback_acceptance import (
    PullbackAcceptanceConfig,
    evaluate_pullback_acceptance,
)
from scripts.run_pullback_acceptance_search import strategy_metrics

OPEN = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)


def _bars(*, continuation: bool = False) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    price = 100.0
    closes = (
        [100.0, 100.1, 100.2, 100.3, 100.4, 100.5, 101.2, 101.5, 101.8]
        if continuation
        else [100.0, 100.1, 100.2, 100.3, 100.4, 100.5, 101.2, 100.75, 101.4]
    )
    volumes = [1_000] * 6 + ([2_000, 1_800, 1_500] if continuation else [2_000, 1_000, 1_500])
    for minute in range(75):
        bucket = minute // 5
        close = closes[bucket] if bucket < len(closes) else 101.4 + 0.08 * (bucket - 8)
        volume = volumes[bucket] if bucket < len(volumes) else 1_200
        high = max(price, close) + (0.03 if bucket != 7 else 0.05)
        low = min(price, close) - (0.03 if bucket != 7 else 0.02)
        rows.append(
            {
                "symbol": "TEST",
                "ts_utc": OPEN + timedelta(minutes=minute),
                "open": price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "vwap": (price + close) / 2,
            }
        )
        price = close
    return pl.DataFrame(rows).with_columns(pl.col("ts_utc").cast(pl.Datetime("ms", "UTC")))


def test_pullback_acceptance_waits_for_reclaim_and_rising_vwap() -> None:
    result = evaluate_pullback_acceptance(_bars(), session_open_utc=OPEN)

    assert result.status == "traded"
    assert result.entry_ts_utc == OPEN + timedelta(minutes=45)
    assert result.pullback_volume_ratio == 0.5
    assert result.vwap_slope is not None and result.vwap_slope > 0


def test_pullback_acceptance_fails_closed_on_incomplete_h30() -> None:
    result = evaluate_pullback_acceptance(
        _bars().filter(pl.col("ts_utc") != OPEN + timedelta(minutes=4)),
        session_open_utc=OPEN,
    )

    assert result.status == "blocked"
    assert result.reason == "h30_incomplete"


def test_pullback_acceptance_keeps_two_percent_all_in_stop() -> None:
    config = PullbackAcceptanceConfig()

    assert config.price_stop_pct + config.stop_slippage_reserve_pct == 0.02


def test_two_bar_acceptance_route_requires_explicit_enablement() -> None:
    baseline = evaluate_pullback_acceptance(_bars(continuation=True), session_open_utc=OPEN)
    enabled = evaluate_pullback_acceptance(
        _bars(continuation=True),
        session_open_utc=OPEN,
        config=PullbackAcceptanceConfig(allow_two_bar_acceptance=True),
    )

    assert baseline.status == "no_trade"
    assert enabled.status == "traded"
    assert enabled.reason == "two_bar_higher_price_accepted"


def test_strategy_metrics_reports_average_win_loss_ratio() -> None:
    metrics = strategy_metrics([0.03, 0.02, -0.01])

    assert metrics["win_rate"] == 2 / 3
    assert metrics["average_win_loss_ratio"] == 2.5
    assert metrics["profit_factor"] == 5.0
