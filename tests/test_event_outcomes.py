from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from kernel.event_features import ResearchSession
from research.event_outcomes import (
    CostAssumptions,
    build_top10_cohorts,
    decision_response,
    event_response,
    quote_replay_costs,
)

OPEN = datetime(2026, 9, 14, 13, 30, tzinfo=UTC)
SESSION = ResearchSession(
    session_id="2026-09-14",
    open_utc=OPEN,
    close_utc=datetime(2026, 9, 14, 20, 0, tzinfo=UTC),
)


def _trades(values: list[tuple[str, datetime, datetime, int, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "symbol": symbol,
                "session_id": SESSION.session_id,
                "source": "test.market",
                "feed": "sip",
                "price_basis": "split",
                "trade_ts": traded_at,
                "available_at": available_at,
                "trade_id": trade_id,
                "price": price,
                "is_valid": True,
            }
            for symbol, traded_at, available_at, trade_id, price in values
        ]
    ).with_columns(
        pl.col("trade_ts", "available_at").cast(pl.Datetime("ns", "UTC"))
    )


def _quotes(
    values: list[tuple[datetime, datetime, float, float, int, int]],
) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "symbol": "TEST",
                "session_id": SESSION.session_id,
                "source": "test.market",
                "feed": "sip",
                "price_basis": "split",
                "ts_utc": quoted_at,
                "available_at": available_at,
                "bid_price": bid,
                "ask_price": ask,
                "bid_size": bid_size,
                "ask_size": ask_size,
                "is_valid": True,
            }
            for quoted_at, available_at, bid, ask, bid_size, ask_size in values
        ]
    ).with_columns(
        pl.col("ts_utc", "available_at").cast(pl.Datetime("ns", "UTC"))
    )


def test_quote_replay_costs_use_ask_bid_and_each_order_minimum_once() -> None:
    result = quote_replay_costs(
        entry_ask=100.02,
        exit_bid=101.00,
        shares=10,
        costs=CostAssumptions(),
    )

    assert result.gross_pnl_usd == pytest.approx(9.8)
    assert result.commission_usd == pytest.approx(0.7)
    assert result.slippage_usd == pytest.approx(2.0102)
    assert result.research_net_pnl_usd == pytest.approx(7.0898)
    assert result.spread_already_in_prices is True


def test_top10_uses_only_boundary_visible_trades_and_stable_tie_order() -> None:
    p10 = OPEN + timedelta(minutes=30)
    p15 = OPEN + timedelta(hours=5, minutes=30)
    trades = _trades(
        [
            ("BBB", p10, p10, 1, 50.0),
            ("BBB", p15, p15, 2, 55.0),
            ("AAA", p10, p10, 3, 100.0),
            ("AAA", p15, p15, 4, 110.0),
            ("MISS", p10, p10 + timedelta(seconds=1), 5, 20.0),
            ("MISS", p15, p15, 6, 25.0),
        ]
    )

    discovery, tradable = build_top10_cohorts(
        trades,
        discovery_symbols=("MISS", "BBB", "AAA"),
        tradable_symbols=("AAA",),
        discovery_cohort_id="discovery-1",
        tradable_cohort_id="tradable-1",
        session=SESSION,
        asof=p15,
        source="test.market",
        feed="sip",
        price_basis="split",
        validity_policy_id="sip-valid.v1",
    )
    reordered, _ = build_top10_cohorts(
        trades.reverse(),
        discovery_symbols=("AAA", "BBB", "MISS"),
        tradable_symbols=("AAA",),
        discovery_cohort_id="discovery-1",
        tradable_cohort_id="tradable-1",
        session=SESSION,
        asof=p15,
        source="test.market",
        feed="sip",
        price_basis="split",
        validity_policy_id="sip-valid.v1",
    )

    assert [(row.symbol, row.rank) for row in discovery.top10] == [
        ("AAA", 1),
        ("BBB", 2),
    ]
    assert next(row for row in discovery.rows if row.symbol == "MISS").status == (
        "missing_price"
    )
    assert tradable.top10[0].symbol == "AAA"
    assert reordered == discovery


def test_event_response_marks_late_horizons_right_censored() -> None:
    recognized = datetime(2026, 9, 14, 18, 40, tzinfo=UTC)
    trades = _trades(
        [
            ("TEST", recognized, recognized, 1, 100.0),
            (
                "TEST",
                recognized + timedelta(minutes=15),
                recognized + timedelta(minutes=15),
                2,
                102.0,
            ),
        ]
    )

    outcomes = event_response(
        trades,
        event_id="event-1",
        symbol="TEST",
        recognized_at=recognized,
        session=SESSION,
        asof=datetime(2026, 9, 14, 19, 0, tzinfo=UTC),
        source="test.market",
        feed="sip",
        price_basis="split",
        validity_policy_id="sip-valid.v1",
    )

    assert outcomes[0].status == "available"
    assert outcomes[0].gross_return == pytest.approx(0.02)
    assert [outcome.status for outcome in outcomes[1:]] == [
        "right_censored",
        "right_censored",
    ]


def test_decision_response_buys_ask_sells_bid_and_applies_research_costs() -> None:
    decision = datetime(2026, 9, 14, 14, 0, tzinfo=UTC)
    target = decision + timedelta(minutes=15)
    quotes = _quotes(
        [
            (
                decision - timedelta(seconds=10),
                decision - timedelta(seconds=10),
                100.00,
                100.02,
                20,
                20,
            ),
            (target, target, 101.00, 101.02, 20, 20),
        ]
    )

    outcome = decision_response(
        quotes,
        event_id="event-1",
        decision_id="decision-1",
        symbol="TEST",
        decision_at=decision,
        session=SESSION,
        asof=target,
        source="test.market",
        feed="sip",
        price_basis="split",
        validity_policy_id="sip-valid.v1",
        shares=10,
        costs=CostAssumptions(),
        horizons=(15,),
    )[0]

    assert outcome.status == "available"
    assert outcome.entry is not None and outcome.entry.side == "ask"
    assert outcome.entry.price == 100.02
    assert outcome.exit is not None and outcome.exit.side == "bid"
    assert outcome.exit.price == 101.00
    assert outcome.costs is not None
    assert outcome.research_net_return == outcome.costs.research_net_return


@pytest.mark.parametrize(
    ("quoted_at", "ask_size", "reason"),
    [
        (datetime(2026, 9, 14, 13, 59, tzinfo=UTC), 20, "stale_quote"),
        (datetime(2026, 9, 14, 13, 59, 50, tzinfo=UTC), 5, "insufficient_displayed_size"),
    ],
)
def test_decision_response_fails_closed_for_stale_or_undersized_quotes(
    quoted_at: datetime,
    ask_size: int,
    reason: str,
) -> None:
    decision = datetime(2026, 9, 14, 14, 0, tzinfo=UTC)
    quote = _quotes([(quoted_at, quoted_at, 100.0, 100.02, 20, ask_size)])

    outcome = decision_response(
        quote,
        event_id="event-1",
        decision_id="decision-1",
        symbol="TEST",
        decision_at=decision,
        session=SESSION,
        asof=decision + timedelta(minutes=16),
        source="test.market",
        feed="sip",
        price_basis="split",
        validity_policy_id="sip-valid.v1",
        shares=10,
        costs=CostAssumptions(),
        horizons=(15,),
    )[0]

    assert outcome.status == "missing_price"
    assert outcome.missing_reason == reason
    assert outcome.gross_return is None
