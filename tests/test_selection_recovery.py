from datetime import UTC, datetime, timedelta

import polars as pl

from research.selection_recovery import h30_recovery_features, recovery_reasons


def _bars(symbol: str, start: datetime, gain: float) -> pl.DataFrame:
    rows = []
    for minute in range(30):
        price = 100 + gain * minute
        rows.append(
            {
                "symbol": symbol,
                "ts_utc": start + timedelta(minutes=minute),
                "open": price,
                "high": price + 0.2,
                "low": price - 0.2,
                "close": price + 0.1,
                "volume": 1000,
                "vwap": price,
            }
        )
    return pl.DataFrame(rows).with_columns(pl.col("ts_utc").dt.replace_time_zone("UTC"))


def test_strong_h30_soft_rejection_is_shadow_selected() -> None:
    start = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)
    features = h30_recovery_features(
        _bars("TEST", start, 0.08),
        _bars("SPY", start, 0.01),
        session_open_utc=start,
    )

    assert features is not None
    assert recovery_reasons(2, features) == ()
    assert recovery_reasons(1, features) == ("catalyst_tier_below_2",)
