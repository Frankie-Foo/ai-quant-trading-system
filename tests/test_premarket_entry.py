from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from execution.premarket_entry import (
    PremarketEntryAction,
    PremarketEntryEngine,
    PremarketEntryPlan,
    PremarketEntryRuntime,
    PremarketEntrySnapshot,
)

NOW = datetime(2026, 7, 29, 11, 0, tzinfo=UTC)


def _plan() -> PremarketEntryPlan:
    return PremarketEntryPlan(
        plan_id="probe-AAPL-20260729",
        symbol="AAPL",
        target_qty=10,
        reference_price=Decimal("100.00"),
    )


def _snapshot(
    seconds: int,
    *,
    bid: str = "100.00",
    ask: str = "100.10",
    filled_qty: int = 0,
    working: bool = False,
) -> PremarketEntrySnapshot:
    at = NOW + timedelta(seconds=seconds)
    return PremarketEntrySnapshot(
        observed_at_utc=at,
        quote_asof_utc=at,
        bid=Decimal(bid),
        ask=Decimal(ask),
        filled_qty=filled_qty,
        order_working=working,
        quote_provenance="alpaca.sip.nbbo",
        data_healthy=True,
        broker_healthy=True,
    )


def test_first_tick_submits_extended_limit_without_crossing_chase_cap() -> None:
    decision = PremarketEntryEngine().evaluate(
        _plan(),
        PremarketEntryRuntime.initial(),
        _snapshot(0, ask="100.80"),
    )

    assert decision.action is PremarketEntryAction.SUBMIT_LIMIT
    assert decision.limit_price == Decimal("100.30")
    assert decision.remaining_qty == 10
    assert decision.runtime.attempt == 0


def test_two_reprices_are_allowed_but_never_above_point_three_percent() -> None:
    engine = PremarketEntryEngine()
    first = engine.evaluate(
        _plan(),
        PremarketEntryRuntime.initial(),
        _snapshot(0, ask="100.10"),
    )
    second = engine.evaluate(
        _plan(),
        first.runtime,
        _snapshot(3, ask="100.20", working=True),
    )
    third = engine.evaluate(
        _plan(),
        second.runtime,
        _snapshot(6, ask="101.00", working=True),
    )

    assert second.action is PremarketEntryAction.CANCEL_REPLACE
    assert second.limit_price == Decimal("100.20")
    assert third.action is PremarketEntryAction.CANCEL_REPLACE
    assert third.limit_price == Decimal("100.30")
    assert third.runtime.attempt == 2


def test_ten_second_ttl_cancels_partial_remainder_and_requires_protection() -> None:
    engine = PremarketEntryEngine()
    first = engine.evaluate(
        _plan(),
        PremarketEntryRuntime.initial(),
        _snapshot(0),
    )

    expired = engine.evaluate(
        _plan(),
        first.runtime,
        _snapshot(10, filled_qty=4, working=True),
    )

    assert expired.action is PremarketEntryAction.CANCEL_REMAINDER
    assert expired.remaining_qty == 6
    assert expired.protection_required_qty == 4


def test_ten_second_ttl_abandons_unfilled_entry() -> None:
    engine = PremarketEntryEngine()
    first = engine.evaluate(
        _plan(),
        PremarketEntryRuntime.initial(),
        _snapshot(0),
    )

    expired = engine.evaluate(
        _plan(),
        first.runtime,
        _snapshot(10, working=True),
    )

    assert expired.action is PremarketEntryAction.ABANDON
    assert expired.remaining_qty == 10


def test_data_or_broker_fault_never_opens_a_new_position() -> None:
    bad = _snapshot(0)
    bad = PremarketEntrySnapshot(
        **{
            **bad.__dict__,
            "data_healthy": False,
        }
    )

    decision = PremarketEntryEngine().evaluate(
        _plan(),
        PremarketEntryRuntime.initial(),
        bad,
    )

    assert decision.action is PremarketEntryAction.ABANDON
    assert decision.blockers == ("market_data_unhealthy",)
