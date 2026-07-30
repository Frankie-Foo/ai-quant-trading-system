from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from execution.synthetic_stop import (
    StopAction,
    SyntheticStopEngine,
    SyntheticStopPlan,
    SyntheticStopRuntime,
    SyntheticStopSnapshot,
)

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def _snapshot(
    seconds: float,
    *,
    bid: str = "99.90",
    ask: str = "99.95",
    last_trade: str = "100.02",
    verified_material_negative: bool = False,
    halt_risk: bool = False,
    data_healthy: bool = True,
    broker_healthy: bool = True,
    filled: bool = False,
) -> SyntheticStopSnapshot:
    observed = NOW + timedelta(seconds=seconds)
    return SyntheticStopSnapshot(
        observed_at_utc=observed,
        quote_asof_utc=observed - timedelta(milliseconds=100),
        bid=Decimal(bid),
        ask=Decimal(ask),
        last_trade=Decimal(last_trade),
        last_trade_asof_utc=observed - timedelta(milliseconds=200),
        quote_provenance="alpaca.sip.nbbo@test",
        trade_provenance="alpaca.sip.trade@test",
        data_healthy=data_healthy,
        broker_healthy=broker_healthy,
        verified_material_negative=verified_material_negative,
        halt_risk=halt_risk,
        filled=filled,
    )


def test_nbbo_bid_must_remain_below_stop_for_two_seconds_before_exit() -> None:
    plan = SyntheticStopPlan(
        plan_id="premarket-AAPL-20260729",
        symbol="AAPL",
        qty=10,
        stop_price=Decimal("100.00"),
    )
    engine = SyntheticStopEngine()

    first = engine.evaluate(plan, SyntheticStopRuntime.initial(), _snapshot(0))
    early = engine.evaluate(plan, first.runtime, _snapshot(1.9))
    triggered = engine.evaluate(plan, early.runtime, _snapshot(2.0))

    assert first.action is StopAction.OBSERVE
    assert first.runtime.below_since_utc == NOW
    assert early.action is StopAction.OBSERVE
    assert triggered.action is StopAction.SUBMIT_EXIT_LIMIT
    assert triggered.limit_price == Decimal("99.65")
    assert triggered.runtime.triggered_at_utc == NOW + timedelta(seconds=2)
    assert triggered.reasons == ("nbbo_below_stop_for_two_seconds",)


def test_trade_and_nbbo_confirmation_triggers_without_two_second_delay() -> None:
    plan = SyntheticStopPlan(
        plan_id="premarket-AAPL-20260729",
        symbol="AAPL",
        qty=10,
        stop_price=Decimal("100.00"),
    )

    decision = SyntheticStopEngine().evaluate(
        plan,
        SyntheticStopRuntime.initial(),
        _snapshot(0, last_trade="99.80"),
    )

    assert decision.action is StopAction.SUBMIT_EXIT_LIMIT
    assert decision.limit_price == Decimal("99.65")
    assert decision.reasons == ("trade_and_nbbo_confirmed_stop",)


def test_verified_negative_bypasses_price_confirmation() -> None:
    plan = SyntheticStopPlan(
        plan_id="premarket-AAPL-20260729",
        symbol="AAPL",
        qty=10,
        stop_price=Decimal("100.00"),
    )

    decision = SyntheticStopEngine().evaluate(
        plan,
        SyntheticStopRuntime.initial(),
        _snapshot(
            0,
            bid="101.00",
            ask="101.05",
            last_trade="101.02",
            verified_material_negative=True,
        ),
    )

    assert decision.action is StopAction.SUBMIT_EXIT_LIMIT
    assert decision.limit_price == Decimal("100.74")
    assert decision.reasons == ("verified_material_negative",)


def test_unfilled_exit_reprices_every_two_seconds_with_bounded_aggression() -> None:
    plan = SyntheticStopPlan(
        plan_id="premarket-AAPL-20260729",
        symbol="AAPL",
        qty=10,
        stop_price=Decimal("100.00"),
    )
    engine = SyntheticStopEngine()
    triggered = engine.evaluate(
        plan,
        SyntheticStopRuntime.initial(),
        _snapshot(0, last_trade="99.80"),
    )

    early = engine.evaluate(plan, triggered.runtime, _snapshot(1.9, bid="99.90"))
    repriced = engine.evaluate(plan, early.runtime, _snapshot(2.0, bid="99.90"))

    assert early.action is StopAction.OBSERVE
    assert repriced.action is StopAction.CANCEL_REPLACE_EXIT
    assert repriced.limit_price == Decimal("99.40")
    assert repriced.runtime.price_attempt == 1
    assert repriced.reasons == ("synthetic_stop_reprice",)


def test_filled_exit_completes_stop_and_never_submits_again() -> None:
    plan = SyntheticStopPlan(
        plan_id="premarket-AAPL-20260729",
        symbol="AAPL",
        qty=10,
        stop_price=Decimal("100.00"),
    )
    engine = SyntheticStopEngine()
    triggered = engine.evaluate(
        plan,
        SyntheticStopRuntime.initial(),
        _snapshot(0, last_trade="99.80"),
    )

    completed = engine.evaluate(
        plan,
        triggered.runtime,
        _snapshot(1, bid="101.00", ask="101.05", filled=True),
    )

    assert completed.action is StopAction.COMPLETE
    assert completed.limit_price is None
    assert completed.reasons == ("synthetic_stop_filled",)


def test_broker_outage_during_hard_exit_alerts_and_keeps_retryable_trigger() -> None:
    plan = SyntheticStopPlan(
        plan_id="premarket-AAPL-20260729",
        symbol="AAPL",
        qty=10,
        stop_price=Decimal("100.00"),
    )

    decision = SyntheticStopEngine().evaluate(
        plan,
        SyntheticStopRuntime.initial(),
        _snapshot(
            0,
            bid="101.00",
            ask="101.05",
            verified_material_negative=True,
            broker_healthy=False,
        ),
    )

    assert decision.action is StopAction.ALERT
    assert decision.limit_price is None
    assert decision.runtime.triggered_at_utc == NOW
    assert decision.blockers == ("broker_unavailable_during_exit",)


def test_market_data_failure_uses_most_aggressive_bounded_exit_limit() -> None:
    plan = SyntheticStopPlan(
        plan_id="premarket-AAPL-20260729",
        symbol="AAPL",
        qty=10,
        stop_price=Decimal("100.00"),
    )

    decision = SyntheticStopEngine().evaluate(
        plan,
        SyntheticStopRuntime.initial(),
        _snapshot(0, data_healthy=False),
    )

    assert decision.action is StopAction.SUBMIT_EXIT_LIMIT
    assert decision.limit_price == Decimal("98.90")
    assert decision.reasons == ("market_data_unhealthy",)
    assert decision.runtime.price_attempt == 2
