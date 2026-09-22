from datetime import UTC, date, datetime, timedelta

import polars as pl

from data_plane.event_universe import (
    EventUniversePolicy,
    SipMarketCapPolicy,
    build_event_universe,
    derive_sip_market_caps,
)

DECISION_AT = datetime(2026, 9, 14, 14, 0, tzinfo=UTC)


def test_derived_cap_cannot_precede_shares_availability() -> None:
    caps = derive_sip_market_caps(
        pl.DataFrame({
            "symbol": ["GOOD"], "shares_outstanding": [20_000_000.0],
            "available_at": [DECISION_AT], "source": ["shares"], "provenance": ["shares"],
        }),
        pl.DataFrame({
            "symbol": ["GOOD"], "price": [100.0],
            "ts_utc": [DECISION_AT - timedelta(seconds=1)],
            "available_at": [DECISION_AT - timedelta(seconds=1)],
            "source": ["alpaca.sip"], "feed": ["sip"],
        }),
        as_of=DECISION_AT,
    )
    before = build_event_universe(
        _reference(), _daily(), caps,
        decision_at=DECISION_AT - timedelta(milliseconds=500),
    ).filter(pl.col("symbol") == "GOOD").row(0, named=True)
    assert before["trade_eligible"] is False
    assert before["market_cap_status"] == "future_unavailable"
    assert caps["available_at"][0] == DECISION_AT


def _reference() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["GOOD", "NOCAP", "ETF", "FUTURE"],
            "instrument_class": ["common_stock", "adr", "etf", "common_stock"],
            "active": [True, True, True, True],
            "reference_asof_date": [date(2026, 9, 11)] * 4,
            "available_at": [datetime(2026, 9, 12, 20, tzinfo=UTC)] * 4,
        }
    )


def _daily() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["GOOD", "NOCAP", "ETF", "FUTURE"],
            "trade_date": [date(2026, 9, 11)] * 4,
            "close": [50.0, 20.0, 100.0, 30.0],
            "available_at": [datetime(2026, 9, 11, 21, tzinfo=UTC)] * 4,
        }
    )


def _caps() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["GOOD", "ETF", "FUTURE"],
            "asof_date": [date(2026, 9, 11)] * 3,
            "market_cap": [2_000_000_000.0, 3_000_000_000.0, 2_000_000_000.0],
            "available_at": [
                datetime(2026, 9, 12, 20, tzinfo=UTC),
                datetime(2026, 9, 12, 20, tzinfo=UTC),
                datetime(2026, 9, 15, 20, tzinfo=UTC),
            ],
            "source": ["provider.direct"] * 3,
            "provenance": ["cap"] * 3,
        }
    )


def test_event_universe_keeps_discovery_rows_but_gates_tradeability() -> None:
    frame = build_event_universe(
        _reference(),
        _daily(),
        _caps(),
        decision_at=DECISION_AT,
        policy=EventUniversePolicy(min_market_cap_usd=1_000_000_000.0),
    )

    assert frame.get_column("symbol").to_list() == ["ETF", "FUTURE", "GOOD", "NOCAP"]
    rows = {row["symbol"]: row for row in frame.iter_rows(named=True)}
    assert rows["GOOD"]["trade_eligible"] is True
    assert rows["NOCAP"]["market_cap_status"] == "missing"
    assert rows["NOCAP"]["trade_eligible"] is False
    assert rows["ETF"]["trade_eligible"] is False
    assert rows["ETF"]["rejection_reason"] == "instrument_class_not_tradable"
    assert rows["FUTURE"]["market_cap_status"] == "future_unavailable"


def test_event_universe_uses_latest_cap_known_at_decision_not_future_revision() -> None:
    caps = pl.concat(
        (
            _caps(),
            pl.DataFrame(
                {
                    "symbol": ["GOOD", "GOOD"],
                    "asof_date": [date(2026, 9, 10), date(2026, 9, 12)],
                    "market_cap": [1_500_000_000.0, 9_000_000_000.0],
                    "available_at": [
                        datetime(2026, 9, 11, 20, tzinfo=UTC),
                        datetime(2026, 9, 15, 20, tzinfo=UTC),
                    ],
                    "source": ["sec.derived", "provider.direct"],
                    "provenance": ["old", "future"],
                }
            ),
        )
    )

    row = build_event_universe(
        _reference(),
        _daily(),
        caps,
        decision_at=DECISION_AT,
        policy=EventUniversePolicy(min_market_cap_usd=1_000_000_000.0),
    ).filter(pl.col("symbol") == "GOOD").row(0, named=True)

    assert row["market_cap"] == 2_000_000_000.0
    assert row["market_cap_source"] == "provider.direct"
    assert row["market_cap_status"] == "available"


def test_event_universe_reports_coverage_from_discovery_denominator() -> None:
    frame = build_event_universe(
        _reference(),
        _daily(),
        _caps(),
        decision_at=DECISION_AT,
        policy=EventUniversePolicy(min_market_cap_usd=1_000_000_000.0),
    )

    assert frame.get_column("discovery_count").unique().to_list() == [4]
    assert frame.get_column("market_cap_covered_count").unique().to_list() == [2]
    assert frame.get_column("market_cap_coverage_ratio").unique().to_list() == [0.5]


def test_sip_market_cap_uses_known_shares_and_fresh_last_trade_only() -> None:
    caps = derive_sip_market_caps(
        pl.DataFrame(
            {
                "symbol": ["GOOD", "FUTURE"],
                "shares_outstanding": [20_000_000.0, 10_000_000.0],
                "available_at": [
                    datetime(2026, 9, 12, 20, tzinfo=UTC),
                    datetime(2026, 9, 12, 20, tzinfo=UTC),
                ],
                "source": ["sec.companyfacts", "sec.companyfacts"],
                "provenance": ["good-shares", "future-shares"],
            }
        ),
        pl.DataFrame(
            {
                "symbol": ["GOOD", "FUTURE"],
                "ts_utc": [
                    datetime(2026, 9, 14, 13, 59, 59, tzinfo=UTC),
                    datetime(2026, 9, 14, 14, 0, 1, tzinfo=UTC),
                ],
                "price": [50.0, 100.0],
                "available_at": [
                    datetime(2026, 9, 14, 13, 59, 59, tzinfo=UTC),
                    datetime(2026, 9, 14, 14, 0, 1, tzinfo=UTC),
                ],
                "source": ["alpaca.sip.rest.trades", "alpaca.sip.rest.trades"],
                "feed": ["sip", "sip"],
            }
        ),
        as_of=DECISION_AT,
        policy=SipMarketCapPolicy(max_trade_age_seconds=2),
    )

    rows = {row["symbol"]: row for row in caps.iter_rows(named=True)}
    assert rows["GOOD"]["market_cap"] == 1_000_000_000.0
    assert rows["GOOD"]["price_source"] == "alpaca.sip.rest.trades"
    assert rows["FUTURE"]["market_cap"] is None
    assert rows["FUTURE"]["market_cap_status"] == "future_trade"


def test_sip_market_cap_uses_fresh_sip_nbbo_when_trade_is_stale() -> None:
    caps = derive_sip_market_caps(
        pl.DataFrame(
            {
                "symbol": ["QUOTE"],
                "shares_outstanding": [20_000_000.0],
                "available_at": [datetime(2026, 9, 12, 20, tzinfo=UTC)],
                "source": ["sec.companyfacts"],
                "provenance": ["quote-shares"],
            }
        ),
        pl.DataFrame(
            {
                "symbol": ["QUOTE"],
                "ts_utc": [datetime(2026, 9, 14, 13, 50, tzinfo=UTC)],
                "price": [40.0],
                "available_at": [datetime(2026, 9, 14, 13, 50, tzinfo=UTC)],
                "source": ["alpaca.sip.rest.trades"],
                "feed": ["sip"],
            }
        ),
        as_of=DECISION_AT,
        policy=SipMarketCapPolicy(max_trade_age_seconds=2),
        quotes=pl.DataFrame(
            {
                "symbol": ["QUOTE"],
                "ts_utc": [datetime(2026, 9, 14, 13, 59, 59, tzinfo=UTC)],
                "bid_price": [49.0],
                "ask_price": [51.0],
                "available_at": [datetime(2026, 9, 14, 13, 59, 59, tzinfo=UTC)],
                "source": ["alpaca.sip.rest.quotes"],
                "feed": ["sip"],
            }
        ),
    )

    row = caps.row(0, named=True)
    assert row["market_cap"] == 1_000_000_000.0
    assert row["price_source"] == "alpaca.sip.rest.quotes.midpoint"


def test_event_universe_accepts_unavailable_sip_cap_rows_without_inventing_cap() -> None:
    caps = derive_sip_market_caps(
        pl.DataFrame(
            {
                "symbol": ["GOOD", "FUTURE"],
                "shares_outstanding": [20_000_000.0, 10_000_000.0],
                "available_at": [datetime(2026, 9, 12, 20, tzinfo=UTC)] * 2,
                "source": ["sec.companyfacts"] * 2,
                "provenance": ["good-shares", "future-shares"],
            }
        ),
        pl.DataFrame(
            {
                "symbol": ["GOOD", "FUTURE"],
                "ts_utc": [
                    datetime(2026, 9, 14, 13, 59, 59, tzinfo=UTC),
                    datetime(2026, 9, 14, 14, 0, 1, tzinfo=UTC),
                ],
                "price": [50.0, 100.0],
                "available_at": [
                    datetime(2026, 9, 14, 13, 59, 59, tzinfo=UTC),
                    datetime(2026, 9, 14, 14, 0, 1, tzinfo=UTC),
                ],
                "source": ["alpaca.sip.rest.trades"] * 2,
                "feed": ["sip"] * 2,
            }
        ),
        as_of=DECISION_AT,
    )

    rows = {
        row["symbol"]: row
        for row in build_event_universe(
            _reference(),
            _daily(),
            caps,
            decision_at=DECISION_AT,
        ).iter_rows(named=True)
    }

    assert rows["GOOD"]["trade_eligible"] is True
    assert rows["FUTURE"]["market_cap_status"] == "future_trade"
    assert rows["FUTURE"]["market_cap"] is None
