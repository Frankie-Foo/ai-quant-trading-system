from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from kernel.intraday_policy import (
    DecisionMetric,
    EntryRoute,
    IntradayPolicy,
    PolicyAction,
    PolicySnapshot,
    TailEvidence,
    TailMode,
)

TRADE_DATE = date(2026, 7, 29)


def _metric(value: float | None, name: str) -> DecisionMetric:
    return DecisionMetric(
        value=value,
        asof_utc=datetime(2026, 7, 29, 11, 4, 59, tzinfo=UTC),
        provenance=f"test.{name}.v1",
    )


def test_catalyst_route_authorizes_bounded_premarket_probe_after_0700_et() -> None:
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=datetime(2026, 7, 29, 11, 5, tzinfo=UTC),
            route=EntryRoute.CATALYST,
            catalyst=_metric(92.0, "catalyst"),
            factor=_metric(68.0, "factor"),
            order_flow=_metric(74.0, "order_flow"),
            execution=_metric(82.0, "execution"),
            right_tail=_metric(76.0, "right_tail"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=False,
            position_fraction=0.0,
            first_target_reward_r=2.8,
            weighted_expected_reward_r=3.4,
            reward_risk_provenance="test.structure_targets.v1",
        )
    )

    assert decision.action is PolicyAction.ENTER_PROBE
    assert decision.max_account_risk_fraction == 0.0015
    assert decision.target_position_fraction == 0.25
    assert decision.reasons == ("catalyst_route_passed",)
    assert decision.blockers == ()


def test_factor_order_flow_route_uses_stricter_thresholds_and_lower_risk() -> None:
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=datetime(2026, 7, 29, 12, 30, tzinfo=UTC),
            route=EntryRoute.FACTOR_ORDER_FLOW,
            catalyst=_metric(None, "catalyst"),
            factor=_metric(66.0, "factor"),
            order_flow=_metric(78.0, "order_flow"),
            execution=_metric(80.0, "execution"),
            right_tail=_metric(65.0, "right_tail"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=False,
            position_fraction=0.0,
            first_target_reward_r=2.7,
            weighted_expected_reward_r=3.2,
            reward_risk_provenance="test.structure_targets.v1",
        )
    )

    assert decision.action is PolicyAction.ENTER_PROBE
    assert decision.max_account_risk_fraction == 0.0010
    assert decision.target_position_fraction == 0.25
    assert decision.reasons == ("factor_order_flow_route_passed",)


def test_premarket_monitoring_before_0700_et_cannot_authorize_entry() -> None:
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=datetime(2026, 7, 29, 10, 59, tzinfo=UTC),
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(
                95.0,
                datetime(2026, 7, 29, 10, 58, tzinfo=UTC),
                "test.catalyst.v1",
            ),
            factor=DecisionMetric(
                90.0,
                datetime(2026, 7, 29, 10, 58, tzinfo=UTC),
                "test.factor.v1",
            ),
            order_flow=DecisionMetric(
                90.0,
                datetime(2026, 7, 29, 10, 58, tzinfo=UTC),
                "test.order_flow.v1",
            ),
            execution=DecisionMetric(
                90.0,
                datetime(2026, 7, 29, 10, 58, tzinfo=UTC),
                "test.execution.v1",
            ),
            right_tail=DecisionMetric(
                90.0,
                datetime(2026, 7, 29, 10, 58, tzinfo=UTC),
                "test.right_tail.v1",
            ),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=False,
            position_fraction=0.0,
        )
    )

    assert decision.action is PolicyAction.OBSERVE
    assert decision.blockers == ("premarket_entry_not_started",)


def test_opening_transition_freezes_new_entries_from_0925_to_0931_et() -> None:
    observed = datetime(2026, 7, 29, 13, 27, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(95.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(90.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(90.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(90.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(90.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=False,
            position_fraction=0.0,
        )
    )

    assert decision.action is PolicyAction.OBSERVE
    assert decision.blockers == ("opening_transition_frozen",)


def test_opening_transition_keeps_existing_probe_but_never_adds() -> None:
    observed = datetime(2026, 7, 29, 13, 27, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(95.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(90.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(90.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(90.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(90.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.25,
        )
    )

    assert decision.action is PolicyAction.HOLD
    assert decision.target_position_fraction == 0.25
    assert decision.reasons == ("opening_transition_hold_only",)


def test_confirmed_probe_can_upgrade_to_half_after_0931_without_averaging_down() -> None:
    observed = datetime(2026, 7, 29, 13, 32, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(92.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(70.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(72.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(82.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(76.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.25,
            average_entry_price=100.0,
            last_price=101.0,
        )
    )

    assert decision.action is PolicyAction.UPGRADE
    assert decision.target_position_fraction == 0.50
    assert decision.reasons == ("opening_confirmation_passed",)


def test_probe_below_average_entry_can_never_be_upgraded() -> None:
    observed = datetime(2026, 7, 29, 13, 32, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(92.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(70.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(72.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(82.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(76.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.25,
            average_entry_price=100.0,
            last_price=99.80,
        )
    )

    assert decision.action is PolicyAction.HOLD
    assert decision.target_position_fraction == 0.25
    assert decision.blockers == ("averaging_down_forbidden",)


def test_catalyst_probe_with_neutral_order_flow_can_wait_until_1000_et() -> None:
    observed = datetime(2026, 7, 29, 13, 40, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(90.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(65.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(52.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(80.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(72.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.25,
            average_entry_price=100.0,
            last_price=100.40,
        )
    )

    assert decision.action is PolicyAction.HOLD
    assert decision.target_position_fraction == 0.25
    assert decision.reasons == ("catalyst_probe_neutral_until_1000",)


def test_unconfirmed_factor_order_flow_probe_exits_at_0935_et() -> None:
    observed = datetime(2026, 7, 29, 13, 40, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.FACTOR_ORDER_FLOW,
            catalyst=DecisionMetric(None, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(72.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(70.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(82.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(68.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.25,
            average_entry_price=100.0,
            last_price=100.40,
        )
    )

    assert decision.action is PolicyAction.EXIT
    assert decision.target_position_fraction == 0.0
    assert decision.reasons == ("factor_probe_unconfirmed_at_0935",)


def test_unconfirmed_catalyst_probe_exits_at_1000_et() -> None:
    observed = datetime(2026, 7, 29, 14, 0, 1, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(90.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(65.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(55.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(82.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(72.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.25,
            average_entry_price=100.0,
            last_price=100.40,
        )
    )

    assert decision.action is PolicyAction.EXIT
    assert decision.target_position_fraction == 0.0
    assert decision.reasons == ("catalyst_probe_unconfirmed_at_1000",)


def test_verified_material_negative_exits_position_during_opening_freeze() -> None:
    observed = datetime(2026, 7, 29, 13, 27, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(92.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(70.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(72.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(82.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(76.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=False,
            material_negative=True,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.25,
            average_entry_price=100.0,
            last_price=100.50,
        )
    )

    assert decision.action is PolicyAction.EXIT
    assert decision.target_position_fraction == 0.0
    assert decision.reasons == ("verified_material_negative",)


def test_required_agent_failure_exits_existing_position() -> None:
    observed = datetime(2026, 7, 29, 13, 40, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(92.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(70.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(72.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(82.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(76.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=False,
            push_healthy=True,
            has_position=True,
            position_fraction=0.25,
            average_entry_price=100.0,
            last_price=100.50,
        )
    )

    assert decision.action is PolicyAction.EXIT
    assert decision.reasons == ("required_agent_unhealthy",)


def test_market_data_failure_exits_existing_position() -> None:
    observed = datetime(2026, 7, 29, 13, 40, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(92.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(70.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(72.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(82.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(76.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=False,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.25,
            average_entry_price=100.0,
            last_price=100.50,
        )
    )

    assert decision.action is PolicyAction.EXIT
    assert decision.reasons == ("market_data_unhealthy",)


def test_push_failure_keeps_protection_but_disables_adds() -> None:
    observed = datetime(2026, 7, 29, 13, 32, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(92.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(70.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(72.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(82.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(76.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=False,
            has_position=True,
            position_fraction=0.25,
            average_entry_price=100.0,
            last_price=100.50,
        )
    )

    assert decision.action is PolicyAction.HOLD
    assert decision.target_position_fraction == 0.25
    assert decision.blockers == ("push_unhealthy_adds_disabled",)


def test_no_new_position_can_be_authorized_after_1200_et() -> None:
    observed = datetime(2026, 7, 29, 16, 1, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(95.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(90.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(90.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(90.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(90.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=False,
            position_fraction=0.0,
        )
    )

    assert decision.action is PolicyAction.OBSERVE
    assert decision.blockers == ("new_entries_cutoff_reached",)


def test_all_positions_exit_at_1200_et() -> None:
    observed = datetime(2026, 7, 29, 17, 0, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(95.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(90.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(90.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(90.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(90.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.80,
            average_entry_price=100.0,
            last_price=110.0,
        )
    )

    assert decision.action is PolicyAction.EXIT
    assert decision.target_position_fraction == 0.0
    assert decision.reasons == ("intraday_force_exit_1200",)


def test_standard_right_tail_score_trims_main_position_to_twenty_percent() -> None:
    observed = datetime(2026, 7, 29, 15, 30, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(82.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(68.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(64.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(80.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(65.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.50,
            average_entry_price=100.0,
            last_price=108.0,
            main_profit_realized=True,
        )
    )

    assert decision.action is PolicyAction.TRIM_TO_TAIL
    assert decision.target_position_fraction == 0.20
    assert decision.tail_mode is TailMode.STANDARD
    assert decision.reasons == ("standard_tail_started",)


def test_realized_main_profit_holds_an_already_sized_tail() -> None:
    observed = datetime(2026, 7, 29, 15, 30, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(82.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(68.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(64.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(80.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(65.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.20,
            average_entry_price=100.0,
            last_price=108.0,
            main_profit_realized=True,
        )
    )

    assert decision.action is PolicyAction.HOLD
    assert decision.target_position_fraction == 0.20
    assert decision.tail_mode is TailMode.STANDARD
    assert decision.reasons == ("standard_tail_active",)


def test_high_right_tail_score_keeps_twenty_five_percent_without_a_plus_plus() -> None:
    observed = datetime(2026, 7, 29, 15, 30, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(88.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(72.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(79.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(85.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(78.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.50,
            average_entry_price=100.0,
            last_price=110.0,
            main_profit_realized=True,
        )
    )

    assert decision.action is PolicyAction.TRIM_TO_TAIL
    assert decision.target_position_fraction == 0.25
    assert decision.tail_mode is TailMode.HIGH_RIGHT_TAIL


def test_a_plus_plus_requires_explicit_approval_before_thirty_percent_tail() -> None:
    observed = datetime(2026, 7, 29, 15, 30, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    common: dict[str, Any] = {
        "trade_date": TRADE_DATE,
        "observed_at_utc": observed,
        "route": EntryRoute.CATALYST,
        "catalyst": DecisionMetric(96.0, metric_time, "test.catalyst.v1"),
        "factor": DecisionMetric(78.0, metric_time, "test.factor.v1"),
        "order_flow": DecisionMetric(88.0, metric_time, "test.order_flow.v1"),
        "execution": DecisionMetric(90.0, metric_time, "test.execution.v1"),
        "right_tail": DecisionMetric(92.0, metric_time, "test.right_tail.v1"),
        "technical_structure_valid": True,
        "negative_news_clear": True,
        "material_negative": False,
        "data_healthy": True,
        "agents_healthy": True,
        "push_healthy": True,
        "has_position": True,
        "position_fraction": 0.50,
        "average_entry_price": 100.0,
        "last_price": 112.0,
        "main_profit_realized": True,
    }

    unapproved = IntradayPolicy().evaluate(PolicySnapshot(**common))
    approved = IntradayPolicy().evaluate(
        PolicySnapshot(**common, a_plus_plus_approved=True)
    )

    assert unapproved.target_position_fraction == 0.25
    assert unapproved.tail_mode is TailMode.HIGH_RIGHT_TAIL
    assert approved.target_position_fraction == 0.30
    assert approved.tail_mode is TailMode.A_PLUS_PLUS


def test_a_plus_plus_approval_cannot_bypass_score_thresholds() -> None:
    observed = datetime(2026, 7, 29, 15, 30, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(94.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(78.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(88.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(90.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(92.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.50,
            average_entry_price=100.0,
            last_price=112.0,
            main_profit_realized=True,
            a_plus_plus_approved=True,
        )
    )

    assert decision.target_position_fraction == 0.25
    assert decision.tail_mode is TailMode.HIGH_RIGHT_TAIL


def test_tail_first_exit_reduces_half_after_two_confirmed_soft_breaks() -> None:
    observed = datetime(2026, 7, 29, 15, 45, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(82.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(68.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(42.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(80.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(65.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.20,
            average_entry_price=100.0,
            last_price=107.0,
            main_profit_realized=True,
            tail=TailEvidence(
                mode=TailMode.STANDARD,
                initial_fraction=0.20,
                reduction_stage=0,
                fifteen_minute_structure_valid=False,
                below_anchored_vwap_5m_bars=0,
                order_flow_below_45_seconds=300,
                failed_reclaim=False,
                current_r=3.2,
                maximum_favorable_excursion_r=4.0,
                chandelier_stop_hit=False,
                hard_breakdown=False,
            ),
        )
    )

    assert decision.action is PolicyAction.REDUCE_TAIL
    assert decision.target_position_fraction == 0.10
    assert decision.reasons == ("tail_two_soft_breaks_confirmed",)


def test_tail_second_exit_closes_remainder_after_failed_reclaim() -> None:
    observed = datetime(2026, 7, 29, 15, 50, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(82.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(68.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(42.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(80.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(65.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.10,
            average_entry_price=100.0,
            last_price=106.0,
            main_profit_realized=True,
            tail=TailEvidence(
                mode=TailMode.STANDARD,
                initial_fraction=0.20,
                reduction_stage=1,
                fifteen_minute_structure_valid=False,
                below_anchored_vwap_5m_bars=3,
                order_flow_below_45_seconds=360,
                failed_reclaim=True,
                current_r=2.8,
                maximum_favorable_excursion_r=4.0,
                chandelier_stop_hit=False,
                hard_breakdown=False,
            ),
        )
    )

    assert decision.action is PolicyAction.EXIT
    assert decision.target_position_fraction == 0.0
    assert decision.reasons == ("tail_failed_reclaim",)


def test_tail_chandelier_stop_exits_all_remaining_tail_immediately() -> None:
    observed = datetime(2026, 7, 29, 15, 50, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(82.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(68.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(60.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(80.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(65.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.20,
            average_entry_price=100.0,
            last_price=106.0,
            main_profit_realized=True,
            tail=TailEvidence(
                mode=TailMode.STANDARD,
                initial_fraction=0.20,
                reduction_stage=0,
                fifteen_minute_structure_valid=True,
                below_anchored_vwap_5m_bars=0,
                order_flow_below_45_seconds=0,
                failed_reclaim=False,
                current_r=2.5,
                maximum_favorable_excursion_r=4.0,
                chandelier_stop_hit=True,
                hard_breakdown=False,
            ),
        )
    )

    assert decision.action is PolicyAction.EXIT
    assert decision.reasons == ("tail_chandelier_stop",)


def test_tail_profit_giveback_guard_caps_right_tail_reversal() -> None:
    observed = datetime(2026, 7, 29, 15, 50, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(82.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(68.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(60.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(80.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(65.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.20,
            average_entry_price=100.0,
            last_price=106.0,
            main_profit_realized=True,
            tail=TailEvidence(
                mode=TailMode.STANDARD,
                initial_fraction=0.20,
                reduction_stage=0,
                fifteen_minute_structure_valid=True,
                below_anchored_vwap_5m_bars=0,
                order_flow_below_45_seconds=0,
                failed_reclaim=False,
                current_r=3.4,
                maximum_favorable_excursion_r=5.0,
                chandelier_stop_hit=False,
                hard_breakdown=False,
            ),
        )
    )

    assert decision.action is PolicyAction.EXIT
    assert decision.reasons == ("tail_profit_giveback_limit",)


def test_tail_hard_breakdown_exits_without_waiting_for_two_soft_signals() -> None:
    observed = datetime(2026, 7, 29, 15, 50, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(82.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(68.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(35.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(80.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(65.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.20,
            average_entry_price=100.0,
            last_price=103.0,
            main_profit_realized=True,
            tail=TailEvidence(
                mode=TailMode.STANDARD,
                initial_fraction=0.20,
                reduction_stage=0,
                fifteen_minute_structure_valid=True,
                below_anchored_vwap_5m_bars=0,
                order_flow_below_45_seconds=0,
                failed_reclaim=False,
                current_r=2.8,
                maximum_favorable_excursion_r=3.5,
                chandelier_stop_hit=False,
                hard_breakdown=True,
            ),
        )
    )

    assert decision.action is PolicyAction.EXIT
    assert decision.reasons == ("tail_hard_breakdown",)


def test_tail_holds_through_a_single_soft_warning() -> None:
    observed = datetime(2026, 7, 29, 15, 50, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(82.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(68.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(42.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(80.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(65.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=True,
            position_fraction=0.20,
            average_entry_price=100.0,
            last_price=108.0,
            main_profit_realized=True,
            tail=TailEvidence(
                mode=TailMode.STANDARD,
                initial_fraction=0.20,
                reduction_stage=0,
                fifteen_minute_structure_valid=True,
                below_anchored_vwap_5m_bars=0,
                order_flow_below_45_seconds=300,
                failed_reclaim=False,
                current_r=3.8,
                maximum_favorable_excursion_r=4.2,
                chandelier_stop_hit=False,
                hard_breakdown=False,
            ),
        )
    )

    assert decision.action is PolicyAction.HOLD
    assert decision.target_position_fraction == 0.20
    assert decision.reasons == ("tail_single_warning_tolerated",)


def test_entry_is_rejected_when_first_structural_target_is_below_two_point_five_r() -> None:
    observed = datetime(2026, 7, 29, 11, 5, tzinfo=UTC)
    metric_time = observed - timedelta(seconds=1)
    decision = IntradayPolicy().evaluate(
        PolicySnapshot(
            trade_date=TRADE_DATE,
            observed_at_utc=observed,
            route=EntryRoute.CATALYST,
            catalyst=DecisionMetric(92.0, metric_time, "test.catalyst.v1"),
            factor=DecisionMetric(70.0, metric_time, "test.factor.v1"),
            order_flow=DecisionMetric(72.0, metric_time, "test.order_flow.v1"),
            execution=DecisionMetric(82.0, metric_time, "test.execution.v1"),
            right_tail=DecisionMetric(76.0, metric_time, "test.right_tail.v1"),
            technical_structure_valid=True,
            negative_news_clear=True,
            material_negative=False,
            data_healthy=True,
            agents_healthy=True,
            push_healthy=True,
            has_position=False,
            position_fraction=0.0,
            first_target_reward_r=2.4,
            weighted_expected_reward_r=3.2,
            reward_risk_provenance="test.structure_targets.v1",
        )
    )

    assert decision.action is PolicyAction.OBSERVE
    assert decision.blockers == ("first_target_below_2_5r",)
