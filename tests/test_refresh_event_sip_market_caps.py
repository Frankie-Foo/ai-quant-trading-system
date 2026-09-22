from datetime import UTC, datetime

import polars as pl

from scripts.refresh_event_sip_market_caps import build_sip_market_cap_snapshot


def test_refresh_snapshot_uses_cached_shares_and_only_fresh_sip_trades() -> None:
    frame = build_sip_market_cap_snapshot(
        pl.DataFrame(
            {
                "symbol": ["GOOD", "FUTURE"],
                "shares_outstanding": [20_000_000.0, 10_000_000.0],
                "available_at": [
                    datetime(2026, 9, 14, 13, 0, tzinfo=UTC),
                    datetime(2026, 9, 14, 14, 1, tzinfo=UTC),
                ],
                "source": ["sec.companyfacts", "sec.companyfacts"],
                "provenance": ["good-shares", "future-shares"],
            }
        ),
        pl.DataFrame(
            {
                "symbol": ["GOOD", "FUTURE"],
                "ts_utc": [
                    datetime(2026, 9, 14, 13, 59, 55, tzinfo=UTC),
                    datetime(2026, 9, 14, 13, 59, 55, tzinfo=UTC),
                ],
                "price": [50.0, 100.0],
                "available_at": [
                    datetime(2026, 9, 14, 14, 0, tzinfo=UTC),
                    datetime(2026, 9, 14, 14, 0, tzinfo=UTC),
                ],
                "source": ["alpaca.sip.rest.trades"] * 2,
                "feed": ["sip"] * 2,
            }
        ),
        as_of=datetime(2026, 9, 14, 14, 0, tzinfo=UTC),
        max_trade_age_seconds=15,
    )

    rows = {row["symbol"]: row for row in frame.iter_rows(named=True)}
    assert rows["GOOD"]["market_cap"] == 1_000_000_000.0
    assert rows["GOOD"]["market_cap_status"] == "available"
    assert rows["FUTURE"]["market_cap"] is None
    assert rows["FUTURE"]["market_cap_status"] == "shares_unavailable"


def test_refresh_snapshot_retains_requested_symbols_missing_from_shares_cache() -> None:
    frame = build_sip_market_cap_snapshot(
        pl.DataFrame(
            {
                "symbol": ["GOOD"],
                "shares_outstanding": [20_000_000.0],
                "available_at": [datetime(2026, 9, 14, 13, 0, tzinfo=UTC)],
                "source": ["sec.companyfacts"],
                "provenance": ["good-shares"],
            }
        ),
        pl.DataFrame(
            {
                "symbol": ["GOOD"],
                "ts_utc": [datetime(2026, 9, 14, 13, 59, 55, tzinfo=UTC)],
                "price": [50.0],
                "available_at": [datetime(2026, 9, 14, 14, 0, tzinfo=UTC)],
                "source": ["alpaca.sip.rest.trades"],
                "feed": ["sip"],
            }
        ),
        as_of=datetime(2026, 9, 14, 14, 0, tzinfo=UTC),
        max_trade_age_seconds=15,
        requested_symbols=("GOOD", "MISS"),
    )

    rows = {row["symbol"]: row for row in frame.iter_rows(named=True)}
    assert rows["GOOD"]["market_cap_status"] == "available"
    assert rows["MISS"]["market_cap"] is None
    assert rows["MISS"]["market_cap_status"] == "shares_unavailable"
    assert rows["MISS"]["provenance"] == "shares_cache_missing"
