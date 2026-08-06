from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from kernel.adaptive_trade_plan import (
    AdaptiveTradePlanEngine,
    BaselineTradePlan,
    PlanAction,
    PlanMode,
    PlanRuntime,
    PlanState,
    PositionFacts,
    RealtimePlanFacts,
)

OPEN = datetime(2026, 7, 28, 13, 30, tzinfo=UTC)


def _plan(*, mode: PlanMode = PlanMode.CATALYST) -> BaselineTradePlan:
    return BaselineTradePlan(
        plan_id="plan-20260728-XYZ",
        symbol="XYZ",
        trade_date=date(2026, 7, 28),
        mode=mode,
        entry_window_end_utc=OPEN + timedelta(hours=2),
        force_exit_utc=OPEN + timedelta(hours=6, minutes=25),
        hard_stop=99.0,
        max_risk_dollars=300.0,
        max_notional=20_000.0,
        probe_fraction=0.25,
        max_spread_ratio=0.0025,
        soft_cooldown=timedelta(minutes=3),
        max_soft_revisions=3,
    )


def _facts(
    minute: int,
    *,
    last_price: float = 101.0,
    one_minute_trigger: bool = True,
    five_minute_confirmed: bool = True,
    fifteen_minute_confirmed: bool = True,
    market_risk_off: bool = False,
    order_flow_imbalance: float | None = 0.28,
    catalyst_score: float | None = 0.82,
    quote_age_seconds: float = 1.0,
    proposed_structural_stop: float | None = None,
    first_target_filled: bool = False,
) -> RealtimePlanFacts:
    observed = OPEN + timedelta(minutes=minute, seconds=15)
    return RealtimePlanFacts(
        observed_at_utc=observed,
        quote_ts_utc=observed - timedelta(seconds=quote_age_seconds),
        bid=last_price - 0.01,
        ask=last_price + 0.01,
        last_price=last_price,
        session_vwap=100.50,
        completed_one_minute_bar_utc=OPEN + timedelta(minutes=minute),
        one_minute_trigger=one_minute_trigger,
        five_minute_confirmed=five_minute_confirmed,
        fifteen_minute_confirmed=fifteen_minute_confirmed,
        green_volume_ratio=1.8,
        relative_strength=0.012,
        benchmark_above_vwap=True,
        sector_above_vwap=True,
        market_risk_off=market_risk_off,
        order_flow_imbalance=order_flow_imbalance,
        catalyst_score=catalyst_score,
        data_complete=True,
        proposed_structural_stop=proposed_structural_stop,
        first_target_filled=first_target_filled,
    )


def test_entry_requires_two_distinct_completed_minute_confirmations() -> None:
    engine = AdaptiveTradePlanEngine()
    runtime = PlanRuntime.initial(_plan())

    armed = engine.evaluate(_plan(), runtime, _facts(6), position=None)
    ready = engine.evaluate(_plan(), armed.runtime, _facts(7), position=None)

    assert armed.action is PlanAction.ARM_ENTRY
    assert armed.material_revision is False
    assert armed.next_state is PlanState.ARMED
    assert ready.action is PlanAction.ENTER_PROBE
    assert ready.material_revision is True
    assert ready.next_state is PlanState.ENTRY_READY
    assert ready.runtime.soft_revision_count == 1
    assert ready.order_authorized is False


def test_repeated_fifteen_second_poll_does_not_recount_same_minute_bar() -> None:
    engine = AdaptiveTradePlanEngine()
    runtime = engine.evaluate(
        _plan(),
        PlanRuntime.initial(_plan()),
        _facts(6),
        position=None,
    ).runtime
    repeated = _facts(6)
    repeated = RealtimePlanFacts(
        **{
            **repeated.__dict__,
            "observed_at_utc": repeated.observed_at_utc + timedelta(seconds=15),
            "quote_ts_utc": repeated.quote_ts_utc + timedelta(seconds=15),
        }
    )

    decision = engine.evaluate(_plan(), runtime, repeated, position=None)

    assert decision.action is PlanAction.NO_ACTION
    assert decision.next_state is PlanState.ARMED
    assert decision.runtime.consecutive_confirmations == 1
    assert decision.runtime.soft_revision_count == 0


def test_market_risk_off_blocks_new_entry_even_when_stock_signals_align() -> None:
    decision = AdaptiveTradePlanEngine().evaluate(
        _plan(),
        PlanRuntime.initial(_plan()),
        _facts(6, market_risk_off=True),
        position=None,
    )

    assert decision.action is PlanAction.NO_ACTION
    assert decision.next_state is PlanState.WATCHING
    assert "market_risk_off" in decision.blockers


def test_factor_plan_requires_observed_order_flow_not_catalyst() -> None:
    plan = _plan(mode=PlanMode.FACTOR)
    engine = AdaptiveTradePlanEngine()
    missing_flow = engine.evaluate(
        plan,
        PlanRuntime.initial(plan),
        _facts(6, catalyst_score=None, order_flow_imbalance=None),
        position=None,
    )
    first = engine.evaluate(
        plan,
        missing_flow.runtime,
        _facts(7, catalyst_score=None, order_flow_imbalance=0.30),
        position=None,
    )
    second = engine.evaluate(
        plan,
        first.runtime,
        _facts(8, catalyst_score=None, order_flow_imbalance=0.31),
        position=None,
    )

    assert "order_flow_unavailable" in missing_flow.blockers
    assert first.action is PlanAction.ARM_ENTRY
    assert second.action is PlanAction.ENTER_PROBE


def test_hard_stop_bypasses_soft_cooldown_and_revision_limit() -> None:
    plan = _plan()
    runtime = PlanRuntime(
        plan_id=plan.plan_id,
        state=PlanState.HOLDING,
        consecutive_confirmations=0,
        last_completed_one_minute_bar_utc=OPEN + timedelta(minutes=30),
        last_material_revision_utc=OPEN + timedelta(minutes=30),
        soft_revision_count=plan.max_soft_revisions,
        protective_stop=100.20,
        revision=7,
    )
    position = PositionFacts(
        symbol="XYZ",
        shares=100,
        average_entry=101.0,
        broker_asof_utc=OPEN + timedelta(minutes=30, seconds=10),
        current_stop=100.20,
    )

    decision = AdaptiveTradePlanEngine().evaluate(
        plan,
        runtime,
        _facts(30, last_price=100.10),
        position=position,
    )

    assert decision.action is PlanAction.EXIT_NOW
    assert decision.next_state is PlanState.EXIT_REQUIRED
    assert decision.material_revision is True
    assert decision.runtime.revision == 8
    assert decision.runtime.soft_revision_count == plan.max_soft_revisions


def test_protective_stop_can_only_tighten_after_first_target() -> None:
    plan = _plan()
    runtime = PlanRuntime(
        plan_id=plan.plan_id,
        state=PlanState.HOLDING,
        consecutive_confirmations=0,
        last_completed_one_minute_bar_utc=OPEN + timedelta(minutes=20),
        last_material_revision_utc=None,
        soft_revision_count=0,
        protective_stop=100.00,
        revision=2,
    )
    position = PositionFacts(
        symbol="XYZ",
        shares=50,
        average_entry=101.0,
        broker_asof_utc=OPEN + timedelta(minutes=21),
        current_stop=100.00,
    )
    tightened = AdaptiveTradePlanEngine().evaluate(
        plan,
        runtime,
        _facts(
            21,
            last_price=102.0,
            proposed_structural_stop=101.25,
            first_target_filled=True,
        ),
        position=position,
    )
    loosen_attempt = AdaptiveTradePlanEngine().evaluate(
        plan,
        tightened.runtime,
        _facts(
            25,
            last_price=102.2,
            proposed_structural_stop=100.80,
            first_target_filled=True,
        ),
        position=PositionFacts(
            symbol="XYZ",
            shares=50,
            average_entry=101.0,
            broker_asof_utc=OPEN + timedelta(minutes=25),
            current_stop=101.25,
        ),
    )

    assert tightened.action is PlanAction.TIGHTEN_STOP
    assert tightened.runtime.protective_stop == 101.25
    assert loosen_attempt.action is PlanAction.NO_ACTION
    assert loosen_attempt.runtime.protective_stop == 101.25


def test_stale_quote_fails_closed_without_changing_plan() -> None:
    plan = _plan()
    decision = AdaptiveTradePlanEngine().evaluate(
        plan,
        PlanRuntime.initial(plan),
        _facts(6, quote_age_seconds=45),
        position=None,
    )

    assert decision.action is PlanAction.NO_ACTION
    assert decision.next_state is PlanState.WATCHING
    assert "quote_stale" in decision.blockers
    assert decision.material_revision is False


def test_force_exit_is_immediate_and_never_authorizes_an_order() -> None:
    plan = _plan()
    at_force = plan.force_exit_utc + timedelta(seconds=1)
    facts = RealtimePlanFacts(
        **{
            **_facts(6).__dict__,
            "observed_at_utc": at_force,
            "quote_ts_utc": at_force - timedelta(seconds=1),
        }
    )
    position = PositionFacts(
        symbol="XYZ",
        shares=25,
        average_entry=101.0,
        broker_asof_utc=at_force,
        current_stop=100.0,
    )

    decision = AdaptiveTradePlanEngine().evaluate(
        plan,
        PlanRuntime.initial(plan),
        facts,
        position=position,
    )

    assert decision.action is PlanAction.EXIT_NOW
    assert decision.next_state is PlanState.EXIT_REQUIRED
    assert "force_exit_time_reached" in decision.reasons
    assert decision.order_authorized is False


def test_broker_flat_after_holding_closes_plan_and_prevents_reentry() -> None:
    plan = _plan()
    runtime = PlanRuntime(
        plan_id=plan.plan_id,
        state=PlanState.HOLDING,
        consecutive_confirmations=2,
        last_completed_one_minute_bar_utc=OPEN + timedelta(minutes=20),
        last_material_revision_utc=OPEN + timedelta(minutes=18),
        soft_revision_count=1,
        protective_stop=100.0,
        revision=2,
    )

    closed = AdaptiveTradePlanEngine().evaluate(
        plan,
        runtime,
        _facts(21),
        position=None,
    )
    repeated = AdaptiveTradePlanEngine().evaluate(
        plan,
        closed.runtime,
        _facts(22),
        position=None,
    )

    assert closed.action is PlanAction.NO_ACTION
    assert closed.next_state is PlanState.CLOSED
    assert closed.material_revision is True
    assert closed.reasons == ("broker_position_flat",)
    assert repeated.next_state is PlanState.CLOSED
    assert repeated.material_revision is False


def test_holding_add_signal_is_sized_and_not_repeated_without_position_change() -> None:
    plan = _plan()
    runtime = PlanRuntime(
        plan_id=plan.plan_id,
        state=PlanState.HOLDING,
        consecutive_confirmations=2,
        last_completed_one_minute_bar_utc=OPEN + timedelta(minutes=7),
        last_material_revision_utc=OPEN + timedelta(minutes=7),
        soft_revision_count=1,
        protective_stop=100.0,
        revision=1,
    )
    position = PositionFacts(
        symbol="XYZ",
        shares=25,
        average_entry=101.0,
        broker_asof_utc=OPEN + timedelta(minutes=11),
        current_stop=100.0,
    )

    allowed = AdaptiveTradePlanEngine().evaluate(
        plan,
        runtime,
        _facts(11, order_flow_imbalance=0.30),
        position=position,
    )
    repeated = AdaptiveTradePlanEngine().evaluate(
        plan,
        allowed.runtime,
        _facts(12, order_flow_imbalance=0.31),
        position=PositionFacts(
            symbol="XYZ",
            shares=25,
            average_entry=101.0,
            broker_asof_utc=OPEN + timedelta(minutes=12),
            current_stop=100.0,
        ),
    )

    assert allowed.action is PlanAction.ALLOW_ADD
    assert allowed.next_state is PlanState.ADD_ALLOWED
    assert allowed.suggested_shares is not None and allowed.suggested_shares > 0
    assert allowed.runtime.last_add_signal_position_shares == 25
    assert repeated.action is PlanAction.NO_ACTION
    assert repeated.next_state is PlanState.ADD_ALLOWED
    assert repeated.suggested_shares is None


def test_entry_probe_has_risk_and_notional_bounded_share_suggestion() -> None:
    engine = AdaptiveTradePlanEngine()
    first = engine.evaluate(
        _plan(),
        PlanRuntime.initial(_plan()),
        _facts(6),
        position=None,
    )

    ready = engine.evaluate(_plan(), first.runtime, _facts(7), position=None)

    assert ready.action is PlanAction.ENTER_PROBE
    assert ready.suggested_shares == 37
    risk = ready.suggested_shares * (_facts(7).ask - _plan().hard_stop)
    notional = ready.suggested_shares * _facts(7).ask
    assert risk <= _plan().max_risk_dollars
    assert notional <= _plan().max_notional
