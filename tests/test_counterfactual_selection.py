from datetime import UTC, datetime, timedelta

import polars as pl

from scripts.analyze_counterfactual_selection import candidate_outcome


def _bars(symbol: str, start: datetime, *, gain: float = 0.0) -> pl.DataFrame:
    rows = []
    for minute in range(390):
        price = 100 + minute * gain
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


def test_candidate_outcome_separates_h30_features_from_forward_labels() -> None:
    start = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)

    result = candidate_outcome(
        _bars("TEST", start, gain=0.01),
        _bars("SPY", start, gain=0.001),
        session_open_utc=start,
    )

    assert result is not None
    assert result["h30_relative_spy"] > 0
    assert result["forward_mfe_pct"] > result["forward_close_pct"]
