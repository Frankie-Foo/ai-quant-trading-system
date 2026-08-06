"""Deterministic intraday policy for entry, position, and tail state transitions.

The module accepts one point-in-time snapshot and returns one auditable decision.
It has no broker, storage, notification, or LLM dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")


class EntryRoute(StrEnum):
    CATALYST = "catalyst"
    FACTOR_ORDER_FLOW = "factor_order_flow"


class PolicyAction(StrEnum):
    OBSERVE = "observe"
    ENTER_PROBE = "enter_probe"
    HOLD = "hold"
    UPGRADE = "upgrade"
    EXIT = "exit"
    TRIM_TO_TAIL = "trim_to_tail"
    REDUCE_TAIL = "reduce_tail"


class TailMode(StrEnum):
    STANDARD = "standard"
    HIGH_RIGHT_TAIL = "high_right_tail"
    A_PLUS_PLUS = "a_plus_plus"


@dataclass(frozen=True)
class TailEvidence:
    mode: TailMode
    initial_fraction: float
    reduction_stage: int
    fifteen_minute_structure_valid: bool
    below_anchored_vwap_5m_bars: int
    order_flow_below_45_seconds: int
    failed_reclaim: bool
    current_r: float
    maximum_favorable_excursion_r: float
    chandelier_stop_hit: bool
    hard_breakdown: bool

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.initial_fraction)
            or not 0 < self.initial_fraction <= 0.30
        ):
            raise ValueError("tail initial_fraction must be in (0, 0.30]")
        if self.reduction_stage not in {0, 1}:
            raise ValueError("tail reduction_stage must be 0 or 1")
        if min(self.below_anchored_vwap_5m_bars, self.order_flow_below_45_seconds) < 0:
            raise ValueError("tail confirmation counters cannot be negative")
        if not math.isfinite(self.current_r) or not math.isfinite(
            self.maximum_favorable_excursion_r
        ):
            raise ValueError("tail R values must be finite")
        if self.maximum_favorable_excursion_r < self.current_r:
            raise ValueError("tail MFE cannot be below current R")


@dataclass(frozen=True)
class DecisionMetric:
    value: float | None
    asof_utc: datetime
    provenance: str

    def __post_init__(self) -> None:
        _require_utc(self.asof_utc, name="metric asof_utc")
        if not self.provenance.strip():
            raise ValueError("metric provenance is required")
        if self.value is not None and (
            not math.isfinite(self.value) or not 0.0 <= self.value <= 100.0
        ):
            raise ValueError("metric value must be between 0 and 100 when available")


@dataclass(frozen=True)
class PolicySnapshot:
    trade_date: date
    observed_at_utc: datetime
    route: EntryRoute
    catalyst: DecisionMetric
    factor: DecisionMetric
    order_flow: DecisionMetric
    execution: DecisionMetric
    right_tail: DecisionMetric
    technical_structure_valid: bool
    negative_news_clear: bool | None
    material_negative: bool
    data_healthy: bool
    agents_healthy: bool
    push_healthy: bool
    has_position: bool
    position_fraction: float
    average_entry_price: float | None = None
    last_price: float | None = None
    approved_account_risk_fraction: float = 0.0035
    main_profit_realized: bool = False
    a_plus_plus_approved: bool = False
    tail: TailEvidence | None = None
    first_target_reward_r: float | None = None
    weighted_expected_reward_r: float | None = None
    reward_risk_provenance: str | None = None

    def __post_init__(self) -> None:
        _require_utc(self.observed_at_utc, name="observed_at_utc")
        if not math.isfinite(self.position_fraction) or not 0.0 <= self.position_fraction <= 1.0:
            raise ValueError("position_fraction must be between 0 and 1")
        if self.has_position != (self.position_fraction > 0):
            raise ValueError("has_position must agree with position_fraction")
        for name, value in (
            ("average_entry_price", self.average_entry_price),
            ("last_price", self.last_price),
        ):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be finite and positive when available")
        if (
            not math.isfinite(self.approved_account_risk_fraction)
            or not 0 < self.approved_account_risk_fraction <= 0.0035
        ):
            raise ValueError("approved_account_risk_fraction must be in (0, 0.0035]")
        for name, value in (
            ("first_target_reward_r", self.first_target_reward_r),
            ("weighted_expected_reward_r", self.weighted_expected_reward_r),
        ):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be finite and positive when available")
        if (
            self.first_target_reward_r is not None
            or self.weighted_expected_reward_r is not None
        ) and not (self.reward_risk_provenance or "").strip():
            raise ValueError("reward/risk provenance is required when values are available")
        for metric in (
            self.catalyst,
            self.factor,
            self.order_flow,
            self.execution,
            self.right_tail,
        ):
            if metric.asof_utc > self.observed_at_utc:
                raise ValueError("decision metric cannot be from the future")


@dataclass(frozen=True)
class PolicyDecision:
    action: PolicyAction
    target_position_fraction: float
    max_account_risk_fraction: float
    reasons: tuple[str, ...]
    blockers: tuple[str, ...]
    tail_mode: TailMode | None = None


class IntradayPolicy:
    """Evaluate a point-in-time snapshot without side effects."""

    def evaluate(self, snapshot: PolicySnapshot) -> PolicyDecision:
        local = snapshot.observed_at_utc.astimezone(NEW_YORK)
        if snapshot.has_position and local.time() >= time(13):
            return PolicyDecision(
                action=PolicyAction.EXIT,
                target_position_fraction=0.0,
                max_account_risk_fraction=0.0,
                reasons=("intraday_force_exit_1300",),
                blockers=(),
            )
        if snapshot.has_position and snapshot.material_negative:
            return PolicyDecision(
                action=PolicyAction.EXIT,
                target_position_fraction=0.0,
                max_account_risk_fraction=0.0,
                reasons=("verified_material_negative",),
                blockers=(),
            )
        if snapshot.has_position and not snapshot.agents_healthy:
            return PolicyDecision(
                action=PolicyAction.EXIT,
                target_position_fraction=0.0,
                max_account_risk_fraction=0.0,
                reasons=("required_agent_unhealthy",),
                blockers=(),
            )
        if snapshot.has_position and not snapshot.data_healthy:
            return PolicyDecision(
                action=PolicyAction.EXIT,
                target_position_fraction=0.0,
                max_account_risk_fraction=0.0,
                reasons=("market_data_unhealthy",),
                blockers=(),
            )
        if (
            snapshot.has_position
            and not snapshot.push_healthy
            and snapshot.technical_structure_valid
        ):
            return PolicyDecision(
                action=PolicyAction.HOLD,
                target_position_fraction=snapshot.position_fraction,
                max_account_risk_fraction=0.0,
                reasons=(),
                blockers=("push_unhealthy_adds_disabled",),
            )
        if snapshot.has_position and snapshot.tail is not None:
            tail = snapshot.tail
            if tail.hard_breakdown:
                return PolicyDecision(
                    action=PolicyAction.EXIT,
                    target_position_fraction=0.0,
                    max_account_risk_fraction=0.0,
                    reasons=("tail_hard_breakdown",),
                    blockers=(),
                    tail_mode=tail.mode,
                )
            if tail.chandelier_stop_hit:
                return PolicyDecision(
                    action=PolicyAction.EXIT,
                    target_position_fraction=0.0,
                    max_account_risk_fraction=0.0,
                    reasons=("tail_chandelier_stop",),
                    blockers=(),
                    tail_mode=tail.mode,
                )
            if tail.reduction_stage == 1 and tail.failed_reclaim:
                return PolicyDecision(
                    action=PolicyAction.EXIT,
                    target_position_fraction=0.0,
                    max_account_risk_fraction=0.0,
                    reasons=("tail_failed_reclaim",),
                    blockers=(),
                    tail_mode=tail.mode,
                )
            allowed_giveback_r = min(
                2.0,
                max(1.0, tail.maximum_favorable_excursion_r * 0.30),
            )
            if (
                tail.maximum_favorable_excursion_r - tail.current_r
                > allowed_giveback_r
            ):
                return PolicyDecision(
                    action=PolicyAction.EXIT,
                    target_position_fraction=0.0,
                    max_account_risk_fraction=0.0,
                    reasons=("tail_profit_giveback_limit",),
                    blockers=(),
                    tail_mode=tail.mode,
                )
            soft_breaks = sum(
                (
                    not tail.fifteen_minute_structure_valid,
                    tail.below_anchored_vwap_5m_bars >= 2,
                    tail.order_flow_below_45_seconds >= 300,
                )
            )
            if tail.reduction_stage == 0 and soft_breaks >= 2:
                return PolicyDecision(
                    action=PolicyAction.REDUCE_TAIL,
                    target_position_fraction=tail.initial_fraction / 2,
                    max_account_risk_fraction=0.0,
                    reasons=("tail_two_soft_breaks_confirmed",),
                    blockers=(),
                    tail_mode=tail.mode,
                )
            return PolicyDecision(
                action=PolicyAction.HOLD,
                target_position_fraction=snapshot.position_fraction,
                max_account_risk_fraction=0.0,
                reasons=(
                    "tail_single_warning_tolerated"
                    if soft_breaks == 1
                    else "tail_trend_intact"
                ,),
                blockers=(),
                tail_mode=tail.mode,
            )
        if snapshot.has_position and snapshot.main_profit_realized:
            score = snapshot.right_tail.value
            a_plus_plus_qualified = (
                score is not None
                and score >= 85.0
                and snapshot.a_plus_plus_approved
                and snapshot.catalyst.value is not None
                and snapshot.catalyst.value >= 95.0
                and snapshot.order_flow.value is not None
                and snapshot.order_flow.value >= 85.0
                and snapshot.execution.value is not None
                and snapshot.execution.value >= 85.0
            )
            if a_plus_plus_qualified:
                tail_mode = TailMode.A_PLUS_PLUS
                tail_fraction = 0.30
                reason = "a_plus_plus_tail_started"
            elif score is not None and score >= 70.0:
                tail_mode = TailMode.HIGH_RIGHT_TAIL
                tail_fraction = 0.25
                reason = "high_right_tail_started"
            else:
                tail_mode = TailMode.STANDARD
                tail_fraction = 0.20
                reason = "standard_tail_started"
            if snapshot.position_fraction > tail_fraction:
                return PolicyDecision(
                    action=PolicyAction.TRIM_TO_TAIL,
                    target_position_fraction=tail_fraction,
                    max_account_risk_fraction=0.0,
                    reasons=(reason,),
                    blockers=(),
                    tail_mode=tail_mode,
                )
            return PolicyDecision(
                action=PolicyAction.HOLD,
                target_position_fraction=snapshot.position_fraction,
                max_account_risk_fraction=0.0,
                reasons=(reason.replace("_started", "_active"),),
                blockers=(),
                tail_mode=tail_mode,
            )
        after_entry_start = local.time() >= time(7)
        if local.time() >= time(12) and not snapshot.has_position:
            return PolicyDecision(
                action=PolicyAction.OBSERVE,
                target_position_fraction=0.0,
                max_account_risk_fraction=0.0,
                reasons=(),
                blockers=("new_entries_cutoff_reached",),
            )
        if not after_entry_start and not snapshot.has_position:
            return PolicyDecision(
                action=PolicyAction.OBSERVE,
                target_position_fraction=0.0,
                max_account_risk_fraction=0.0,
                reasons=(),
                blockers=("premarket_entry_not_started",),
            )
        if time(9, 25) <= local.time() < time(9, 31) and not snapshot.has_position:
            return PolicyDecision(
                action=PolicyAction.OBSERVE,
                target_position_fraction=0.0,
                max_account_risk_fraction=0.0,
                reasons=(),
                blockers=("opening_transition_frozen",),
            )
        if not snapshot.has_position:
            if (
                snapshot.first_target_reward_r is None
                or snapshot.weighted_expected_reward_r is None
                or not (snapshot.reward_risk_provenance or "").strip()
            ):
                return PolicyDecision(
                    action=PolicyAction.OBSERVE,
                    target_position_fraction=0.0,
                    max_account_risk_fraction=0.0,
                    reasons=(),
                    blockers=("reward_risk_unavailable",),
                )
            if snapshot.first_target_reward_r < 2.5:
                return PolicyDecision(
                    action=PolicyAction.OBSERVE,
                    target_position_fraction=0.0,
                    max_account_risk_fraction=0.0,
                    reasons=(),
                    blockers=("first_target_below_2_5r",),
                )
            if snapshot.weighted_expected_reward_r < 3.0:
                return PolicyDecision(
                    action=PolicyAction.OBSERVE,
                    target_position_fraction=0.0,
                    max_account_risk_fraction=0.0,
                    reasons=(),
                    blockers=("weighted_expected_reward_below_3r",),
                )
        if (
            time(9, 25) <= local.time() < time(9, 31)
            and snapshot.has_position
            and not snapshot.material_negative
            and snapshot.data_healthy
            and snapshot.agents_healthy
            and snapshot.push_healthy
            and snapshot.technical_structure_valid
        ):
            return PolicyDecision(
                action=PolicyAction.HOLD,
                target_position_fraction=snapshot.position_fraction,
                max_account_risk_fraction=0.0,
                reasons=("opening_transition_hold_only",),
                blockers=(),
            )
        opening_confirmation_passed = (
            local.time() >= time(9, 31)
            and snapshot.has_position
            and snapshot.position_fraction == 0.25
            and snapshot.average_entry_price is not None
            and snapshot.last_price is not None
            and snapshot.last_price >= snapshot.average_entry_price
            and snapshot.route is EntryRoute.CATALYST
            and snapshot.catalyst.value is not None
            and snapshot.catalyst.value >= 75.0
            and snapshot.order_flow.value is not None
            and snapshot.order_flow.value >= 60.0
            and snapshot.execution.value is not None
            and snapshot.execution.value >= 70.0
            and snapshot.technical_structure_valid
            and snapshot.negative_news_clear is True
            and not snapshot.material_negative
            and snapshot.data_healthy
            and snapshot.agents_healthy
            and snapshot.push_healthy
        )
        if opening_confirmation_passed:
            return PolicyDecision(
                action=PolicyAction.UPGRADE,
                target_position_fraction=0.50,
                max_account_risk_fraction=snapshot.approved_account_risk_fraction,
                reasons=("opening_confirmation_passed",),
                blockers=(),
            )
        if (
            local.time() >= time(10)
            and snapshot.has_position
            and snapshot.position_fraction == 0.25
            and snapshot.route is EntryRoute.CATALYST
        ):
            return PolicyDecision(
                action=PolicyAction.EXIT,
                target_position_fraction=0.0,
                max_account_risk_fraction=0.0,
                reasons=("catalyst_probe_unconfirmed_at_1000",),
                blockers=(),
            )
        if (
            local.time() >= time(9, 31)
            and snapshot.has_position
            and snapshot.average_entry_price is not None
            and snapshot.last_price is not None
            and snapshot.last_price < snapshot.average_entry_price
            and snapshot.technical_structure_valid
            and not snapshot.material_negative
        ):
            return PolicyDecision(
                action=PolicyAction.HOLD,
                target_position_fraction=snapshot.position_fraction,
                max_account_risk_fraction=0.0,
                reasons=(),
                blockers=("averaging_down_forbidden",),
            )
        if (
            time(9, 35) <= local.time() < time(10)
            and snapshot.has_position
            and snapshot.position_fraction == 0.25
            and snapshot.route is EntryRoute.CATALYST
            and snapshot.catalyst.value is not None
            and snapshot.catalyst.value >= 75.0
            and snapshot.order_flow.value is not None
            and 45.0 <= snapshot.order_flow.value < 60.0
            and snapshot.technical_structure_valid
            and snapshot.negative_news_clear is True
            and not snapshot.material_negative
            and snapshot.data_healthy
            and snapshot.agents_healthy
            and snapshot.push_healthy
        ):
            return PolicyDecision(
                action=PolicyAction.HOLD,
                target_position_fraction=0.25,
                max_account_risk_fraction=0.0015,
                reasons=("catalyst_probe_neutral_until_1000",),
                blockers=(),
            )
        if (
            local.time() >= time(9, 35)
            and snapshot.has_position
            and snapshot.position_fraction == 0.25
            and snapshot.route is EntryRoute.FACTOR_ORDER_FLOW
            and (
                snapshot.factor.value is None
                or snapshot.factor.value < 65.0
                or snapshot.order_flow.value is None
                or snapshot.order_flow.value < 75.0
                or snapshot.execution.value is None
                or snapshot.execution.value < 75.0
                or not snapshot.technical_structure_valid
            )
        ):
            return PolicyDecision(
                action=PolicyAction.EXIT,
                target_position_fraction=0.0,
                max_account_risk_fraction=0.0,
                reasons=("factor_probe_unconfirmed_at_0935",),
                blockers=(),
            )
        common_passed = (
            snapshot.technical_structure_valid
            and snapshot.negative_news_clear is True
            and not snapshot.material_negative
            and snapshot.data_healthy
            and snapshot.agents_healthy
            and snapshot.push_healthy
            and not snapshot.has_position
        )
        catalyst_passed = (
            snapshot.route is EntryRoute.CATALYST
            and snapshot.catalyst.value is not None
            and snapshot.catalyst.value >= 75.0
            and snapshot.order_flow.value is not None
            and snapshot.order_flow.value >= 60.0
            and snapshot.execution.value is not None
            and snapshot.execution.value >= 70.0
            and common_passed
        )
        if after_entry_start and catalyst_passed:
            return PolicyDecision(
                action=PolicyAction.ENTER_PROBE,
                target_position_fraction=0.25,
                max_account_risk_fraction=0.0015,
                reasons=("catalyst_route_passed",),
                blockers=(),
            )
        factor_order_flow_passed = (
            snapshot.route is EntryRoute.FACTOR_ORDER_FLOW
            and snapshot.factor.value is not None
            and snapshot.factor.value >= 65.0
            and snapshot.order_flow.value is not None
            and snapshot.order_flow.value >= 75.0
            and snapshot.execution.value is not None
            and snapshot.execution.value >= 75.0
            and common_passed
        )
        if after_entry_start and factor_order_flow_passed:
            return PolicyDecision(
                action=PolicyAction.ENTER_PROBE,
                target_position_fraction=0.25,
                max_account_risk_fraction=0.0010,
                reasons=("factor_order_flow_route_passed",),
                blockers=(),
            )
        blockers: list[str] = []
        if not snapshot.technical_structure_valid:
            blockers.append("technical_structure_invalid")
        if snapshot.negative_news_clear is not True:
            blockers.append("negative_news_not_cleared")
        if snapshot.material_negative:
            blockers.append("verified_material_negative")
        if not snapshot.data_healthy:
            blockers.append("market_data_unhealthy")
        if not snapshot.agents_healthy:
            blockers.append("required_agent_unhealthy")
        if not snapshot.push_healthy:
            blockers.append("push_unhealthy")
        if snapshot.route is EntryRoute.CATALYST:
            if snapshot.catalyst.value is None:
                blockers.append("catalyst_score_unavailable")
            elif snapshot.catalyst.value < 75.0:
                blockers.append("catalyst_score_below_75")
            if snapshot.order_flow.value is None:
                blockers.append("order_flow_score_unavailable")
            elif snapshot.order_flow.value < 60.0:
                blockers.append("order_flow_score_below_60")
            if snapshot.execution.value is None:
                blockers.append("execution_score_unavailable")
            elif snapshot.execution.value < 70.0:
                blockers.append("execution_score_below_70")
        else:
            if snapshot.factor.value is None:
                blockers.append("factor_score_unavailable")
            elif snapshot.factor.value < 65.0:
                blockers.append("factor_score_below_65")
            if snapshot.order_flow.value is None:
                blockers.append("order_flow_score_unavailable")
            elif snapshot.order_flow.value < 75.0:
                blockers.append("order_flow_score_below_75")
            if snapshot.execution.value is None:
                blockers.append("execution_score_unavailable")
            elif snapshot.execution.value < 75.0:
                blockers.append("execution_score_below_75")
        if not blockers:
            blockers.append("entry_conditions_not_met")
        return PolicyDecision(
            action=PolicyAction.OBSERVE,
            target_position_fraction=snapshot.position_fraction,
            max_account_risk_fraction=0.0,
            reasons=(),
            blockers=tuple(blockers),
        )


def _require_utc(value: datetime, *, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
