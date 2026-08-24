from datetime import UTC, datetime, timedelta

import polars as pl

from scripts.run_modern_funnel_stage import (
    evaluate_open_confirmation,
    evaluate_second_wave,
)

NOW = datetime(2026, 8, 24, 13, 25, tzinfo=UTC)


def _candidate(symbol: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "forward_market_cap": 2_000_000_000,
        "premarket_return": 0.05,
    }


def test_second_wave_keeps_only_liquid_tight_names_above_vwap() -> None:
    bars = pl.DataFrame(
        {
            "symbol": ["GOOD", "GOOD", "WIDE"],
            "ts_utc": [NOW - timedelta(minutes=2), NOW - timedelta(minutes=1), NOW],
            "close": [10.0, 10.2, 10.0],
            "volume": [100_000, 100_000, 200_000],
        }
    )
    quotes = pl.DataFrame(
        {
            "symbol": ["GOOD", "WIDE"],
            "ts_utc": [NOW, NOW],
            "bid_price": [10.19, 9.90],
            "ask_price": [10.20, 10.10],
        }
    )

    kept, rejected = evaluate_second_wave(
        [_candidate("GOOD"), _candidate("WIDE")], bars, quotes
    )

    assert [row["symbol"] for row in kept] == ["GOOD"]
    assert [row["symbol"] for row in rejected] == ["WIDE"]
    assert "点差" in str(rejected[0]["reasons"])


def test_open_confirmation_requires_complete_positive_accepted_five_minutes() -> None:
    rows: list[dict[str, object]] = []
    for index in range(5):
        rows.append(
            {
                "symbol": "PASS",
                "ts_utc": NOW + timedelta(minutes=index),
                "open": 10.0 + index * 0.05,
                "high": 10.2 + index * 0.05,
                "low": 9.95 + index * 0.05,
                "close": 10.15 + index * 0.05,
                "volume": 100_000,
            }
        )
        rows.append(
            {
                "symbol": "FAIL",
                "ts_utc": NOW + timedelta(minutes=index),
                "open": 10.0 - index * 0.05,
                "high": 10.05 - index * 0.05,
                "low": 9.8 - index * 0.05,
                "close": 9.85 - index * 0.05,
                "volume": 100_000,
            }
        )

    kept, rejected = evaluate_open_confirmation(
        [_candidate("PASS"), _candidate("FAIL")], pl.DataFrame(rows)
    )

    assert [row["symbol"] for row in kept] == ["PASS"]
    assert [row["symbol"] for row in rejected] == ["FAIL"]
    assert rejected[0]["reasons"]
