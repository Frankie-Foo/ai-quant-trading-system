"""Deterministic event-driven trade-plan revisions.

The module owns plan state, confirmation, cooldown, hysteresis and immutable
risk constraints behind one interface.  It is advisory-only and deliberately
has no broker or notification dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum


class PlanMode(StrEnum):
    CATALYST = "catalyst"
    FACTOR = "factor"


class PlanState(StrEnum):
    WATCHING = "watching"
    ARMED = "armed"
    ENTRY_READY = "entry_ready"
    HOLDING = "holding"
    ADD_ALLOWED = "add_allowed"
    REDUCE_REQUIRED = "reduce_required"
    EXIT_REQUIRED = "exit_required"
    ABANDONED = "abandoned"
    CLOSED = "closed"


class PlanAction(StrEnum):
    NO_ACTION = "no_action"
    ARM_ENTRY = "arm_entry"
    ENTER_PROBE = "enter_probe"
    ALLOW_ADD = "allow_add"
    REDUCE = "reduce"
    TIGHTEN_STOP = "tighten_stop"
    EXIT_NOW = "exit_now"
    ABANDON = "abandon"


def _require_utc(value: datetime, *, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _finite_positive(value: float, *, name: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True)
class BaselineTradePlan:
    """Immutable owner-approved risk envelope for one symbol and session."""

    plan_id: str
    symbol: str
    trade_date: date
    mode: PlanMode
    entry_window_end_utc: datetime
    force_exit_utc: datetime
    hard_stop: float
    max_risk_dollars: float
    max_notional: float
    probe_fraction: float
    max_spread_ratio: float
    soft_cooldown: timedelta
    max_soft_revisions: int

    def __post_init__(self) -> None:
        _require_utc(self.entry_window_end_utc, name="entry_window_end_utc")
        _require_utc(self.force_exit_utc, name="force_exit_utc")
        if not self.plan_id.strip() or not self.symbol.strip():
            raise ValueError("plan_id and symbol are required")
        if self.symbol != self.symbol.strip().upper():
            raise ValueError("symbol must be normalized uppercase")
        if self.force_exit_utc <= self.entry_window_end_utc:
            raise ValueError("force exit must be after entry window")
        for name, value in (
            ("hard_stop", self.hard_stop),
            ("max_risk_dollars", self.max_risk_dollars),
            ("max_notional", self.max_notional),
            ("probe_fraction", self.probe_fraction),
            ("max_spread_ratio", self.max_spread_ratio),
        ):
            _finite_positive(value, name=name)
        if self.probe_fraction > 1:
            raise ValueError("probe_fraction must be in (0, 1]")
        if self.soft_cooldown < timedelta(0):
            raise ValueError("soft_cooldown cannot be negative")
        if self.max_soft_revisions < 1:
            raise ValueError("max_soft_revisions must be positive")


@dataclass(frozen=True)
class PositionFacts:
    """Broker-observed position; local UI state is never accepted as authority."""

    symbol: str
    shares: int
    average_entry: float
    broker_asof_utc: datetime
    current_stop: float

    def __post_init__(self) -> None:
        _require_utc(self.broker_asof_utc, name="broker_asof_utc")
        if self.symbol != self.symbol.strip().upper():
            raise ValueError("position symbol must be normalized uppercase")
        if self.shares <= 0:
            raise ValueError("position shares must be positive")
        _finite_positive(self.average_entry, name="average_entry")
        _finite_positive(self.current_stop, name="current_stop")


@dataclass(frozen=True)
class RealtimePlanFacts:
    """Point-in-time facts known when the engine evaluates one plan."""

    observed_at_utc: datetime
    quote_ts_utc: datetime
    bid: float
    ask: float
    last_price: float
    session_vwap: float | None
    completed_one_minute_bar_utc: datetime | None
    one_minute_trigger: bool
    five_minute_confirmed: bool
    fifteen_minute_confirmed: bool
    green_volume_ratio: float | None
    relative_strength: float | None
    benchmark_above_vwap: bool
    sector_above_vwap: bool
    market_risk_off: bool
    order_flow_imbalance: float | None
    catalyst_score: float | None
    data_complete: bool
    proposed_structural_stop: float | None = None
    first_target_filled: bool = False

    def __post_init__(self) -> None:
        _require_utc(self.observed_at_utc, name="observed_at_utc")
        _require_utc(self.quote_ts_utc, name="quote_ts_utc")
        if self.completed_one_minute_bar_utc is not None:
            _require_utc(
                self.completed_one_minute_bar_utc,
                name="completed_one_minute_bar_utc",
            )
            if self.completed_one_minute_bar_utc > self.observed_at_utc:
                raise ValueError("completed bar cannot be in the future")
        for name, value in (
            ("bid", self.bid),
            ("ask", self.ask),
            ("last_price", self.last_price),
        ):
            _finite_positive(value, name=name)
        if self.ask < self.bid:
            raise ValueError("quote cannot be crossed")
        if self.quote_ts_utc > self.observed_at_utc:
            raise ValueError("quote cannot be from the future")
        for name, optional_value in (
            ("session_vwap", self.session_vwap),
            ("green_volume_ratio", self.green_volume_ratio),
            ("relative_strength", self.relative_strength),
            ("order_flow_imbalance", self.order_flow_imbalance),
            ("catalyst_score", self.catalyst_score),
            ("proposed_structural_stop", self.proposed_structural_stop),
        ):
            if optional_value is not None and not math.isfinite(optional_value):
                raise ValueError(f"{name} must be finite when present")

    @property
    def midpoint(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_ratio(self) -> float:
        return (self.ask - self.bid) / self.midpoint

    @property
    def quote_age_seconds(self) -> float:
        return (self.observed_at_utc - self.quote_ts_utc).total_seconds()


@dataclass(frozen=True)
class PlanRuntime:
    plan_id: str
    state: PlanState
    consecutive_confirmations: int
    last_completed_one_minute_bar_utc: datetime | None
    last_material_revision_utc: datetime | None
    soft_revision_count: int
    protective_stop: float
    revision: int
    last_add_signal_position_shares: int | None = None

    @classmethod
    def initial(cls, plan: BaselineTradePlan) -> PlanRuntime:
        return cls(
            plan_id=plan.plan_id,
            state=PlanState.WATCHING,
            consecutive_confirmations=0,
            last_completed_one_minute_bar_utc=None,
            last_material_revision_utc=None,
            soft_revision_count=0,
            protective_stop=plan.hard_stop,
            revision=0,
            last_add_signal_position_shares=None,
        )

    def __post_init__(self) -> None:
        if not self.plan_id.strip():
            raise ValueError("runtime plan_id is required")
        if self.last_completed_one_minute_bar_utc is not None:
            _require_utc(
                self.last_completed_one_minute_bar_utc,
                name="last_completed_one_minute_bar_utc",
            )
        if self.last_material_revision_utc is not None:
            _require_utc(
                self.last_material_revision_utc,
                name="last_material_revision_utc",
            )
        if min(self.consecutive_confirmations, self.soft_revision_count, self.revision) < 0:
            raise ValueError("runtime counters cannot be negative")
        if (
            self.last_add_signal_position_shares is not None
            and self.last_add_signal_position_shares <= 0
        ):
            raise ValueError("last add-signal position shares must be positive")
        _finite_positive(self.protective_stop, name="protective_stop")


@dataclass(frozen=True)
class PlanDecision:
    plan_id: str
    symbol: str
    observed_at_utc: datetime
    action: PlanAction
    prior_state: PlanState
    next_state: PlanState
    material_revision: bool
    reasons: tuple[str, ...]
    blockers: tuple[str, ...]
    runtime: PlanRuntime
    suggested_shares: int | None = None
    order_authorized: bool = False


class AdaptiveTradePlanEngine:
    """Evaluate one immutable baseline plan against one point-in-time fact set."""

    _MAX_QUOTE_AGE_SECONDS = 30.0
    _MIN_GREEN_VOLUME_RATIO = 1.5
    _MIN_RELATIVE_STRENGTH = 0.0
    _MIN_ORDER_FLOW_IMBALANCE = 0.20
    _MIN_CATALYST_SCORE = 0.70

    def evaluate(
        self,
        plan: BaselineTradePlan,
        runtime: PlanRuntime,
        facts: RealtimePlanFacts,
        *,
        position: PositionFacts | None,
    ) -> PlanDecision:
        if runtime.plan_id != plan.plan_id:
            raise ValueError("runtime does not belong to plan")
        if position is not None and position.symbol != plan.symbol:
            raise ValueError("position does not belong to plan symbol")
        prior_state = runtime.state
        synchronized = runtime
        if position is not None and runtime.state in {
            PlanState.WATCHING,
            PlanState.ARMED,
            PlanState.ENTRY_READY,
        }:
            synchronized = replace(runtime, state=PlanState.HOLDING)

        position_states = {
            PlanState.HOLDING,
            PlanState.ADD_ALLOWED,
            PlanState.REDUCE_REQUIRED,
            PlanState.EXIT_REQUIRED,
        }
        if position is None and synchronized.state in position_states:
            return self._decision(
                plan,
                synchronized,
                facts,
                prior_state=prior_state,
                action=PlanAction.NO_ACTION,
                next_state=PlanState.CLOSED,
                material=True,
                reasons=("broker_position_flat",),
                risk_revision=True,
            )
        if position is None and synchronized.state is PlanState.CLOSED:
            return self._decision(
                plan,
                synchronized,
                facts,
                prior_state=prior_state,
                next_state=PlanState.CLOSED,
            )

        if position is not None and facts.observed_at_utc >= plan.force_exit_utc:
            return self._decision(
                plan,
                synchronized,
                facts,
                prior_state=prior_state,
                action=PlanAction.EXIT_NOW,
                next_state=PlanState.EXIT_REQUIRED,
                material=True,
                reasons=("force_exit_time_reached",),
                risk_revision=True,
            )

        quote_blockers = self._quote_blockers(plan, facts)
        if quote_blockers:
            return self._decision(
                plan,
                synchronized,
                facts,
                prior_state=prior_state,
                blockers=quote_blockers,
            )

        if position is not None:
            effective_stop = max(
                plan.hard_stop,
                synchronized.protective_stop,
                position.current_stop,
            )
            if facts.bid <= effective_stop:
                return self._decision(
                    plan,
                    replace(synchronized, protective_stop=effective_stop),
                    facts,
                    prior_state=prior_state,
                    action=PlanAction.EXIT_NOW,
                    next_state=PlanState.EXIT_REQUIRED,
                    material=True,
                    reasons=("protective_stop_reached",),
                    risk_revision=True,
                )
            proposed = facts.proposed_structural_stop
            if (
                facts.first_target_filled
                and proposed is not None
                and effective_stop < proposed < facts.bid
            ):
                return self._decision(
                    plan,
                    replace(synchronized, protective_stop=proposed),
                    facts,
                    prior_state=prior_state,
                    action=PlanAction.TIGHTEN_STOP,
                    next_state=PlanState.HOLDING,
                    material=True,
                    reasons=("first_target_filled", "structural_stop_tightened"),
                    risk_revision=True,
                )
            deterioration = (
                facts.session_vwap is not None
                and facts.last_price < facts.session_vwap
                and not facts.five_minute_confirmed
                and (
                    facts.order_flow_imbalance is None
                    or facts.order_flow_imbalance < -self._MIN_ORDER_FLOW_IMBALANCE
                )
            )
            if deterioration:
                return self._decision(
                    plan,
                    synchronized,
                    facts,
                    prior_state=prior_state,
                    action=PlanAction.REDUCE,
                    next_state=PlanState.REDUCE_REQUIRED,
                    material=True,
                    reasons=("vwap_lost", "five_minute_structure_failed"),
                    risk_revision=True,
                )
            add_blockers = list(self._entry_blockers(plan, facts))
            if facts.first_target_filled:
                add_blockers.append("first_target_already_filled")
            if (
                facts.order_flow_imbalance is None
                or facts.order_flow_imbalance < self._MIN_ORDER_FLOW_IMBALANCE
            ):
                if "order_flow_unavailable" not in add_blockers and (
                    "order_flow_below_threshold" not in add_blockers
                ):
                    add_blockers.append(
                        "order_flow_unavailable"
                        if facts.order_flow_imbalance is None
                        else "order_flow_below_threshold"
                    )
            capacity = self._risk_capacity(
                plan,
                facts,
                position=position,
                effective_stop=effective_stop,
            )
            if capacity < 1:
                add_blockers.append("risk_capacity_exhausted")
            completed = facts.completed_one_minute_bar_utc
            same_position_as_last_signal = (
                synchronized.last_add_signal_position_shares is not None
                and position.shares
                <= synchronized.last_add_signal_position_shares
            )
            if not add_blockers and same_position_as_last_signal:
                if synchronized.state is PlanState.ADD_ALLOWED:
                    return self._decision(
                        plan,
                        synchronized,
                        facts,
                        prior_state=prior_state,
                        next_state=PlanState.ADD_ALLOWED,
                    )
                add_blockers.append("awaiting_position_increase_after_add_signal")
            if add_blockers:
                return self._decision(
                    plan,
                    synchronized,
                    facts,
                    prior_state=prior_state,
                    next_state=PlanState.HOLDING,
                    blockers=tuple(add_blockers),
                )
            if (
                completed is not None
                and completed != synchronized.last_completed_one_minute_bar_utc
            ):
                soft_blocker = self._soft_revision_blocker(
                    plan,
                    synchronized,
                    facts,
                )
                if soft_blocker is not None:
                    return self._decision(
                        plan,
                        synchronized,
                        facts,
                        prior_state=prior_state,
                        next_state=PlanState.HOLDING,
                        blockers=(soft_blocker,),
                    )
                add_runtime = replace(
                    synchronized,
                    last_completed_one_minute_bar_utc=completed,
                    last_add_signal_position_shares=position.shares,
                )
                return self._decision(
                    plan,
                    add_runtime,
                    facts,
                    prior_state=prior_state,
                    action=PlanAction.ALLOW_ADD,
                    next_state=PlanState.ADD_ALLOWED,
                    material=True,
                    reasons=("holding_confluence_reconfirmed", "risk_capacity_available"),
                    soft_revision=True,
                    suggested_shares=self._probe_size(plan, capacity),
                )
            return self._decision(
                plan,
                synchronized,
                facts,
                prior_state=prior_state,
                next_state=PlanState.HOLDING,
            )

        if facts.observed_at_utc >= plan.entry_window_end_utc:
            if synchronized.state is PlanState.ABANDONED:
                return self._decision(
                    plan,
                    synchronized,
                    facts,
                    prior_state=prior_state,
                    next_state=PlanState.ABANDONED,
                )
            return self._decision(
                plan,
                synchronized,
                facts,
                prior_state=prior_state,
                action=PlanAction.ABANDON,
                next_state=PlanState.ABANDONED,
                material=True,
                reasons=("entry_window_expired",),
                risk_revision=True,
            )

        blockers = self._entry_blockers(plan, facts)
        entry_capacity = self._risk_capacity(
            plan,
            facts,
            position=None,
            effective_stop=plan.hard_stop,
        )
        if entry_capacity < 1:
            blockers = (*blockers, "risk_capacity_exhausted")
        if blockers:
            disarmed = replace(
                synchronized,
                state=PlanState.WATCHING,
                consecutive_confirmations=0,
            )
            return self._decision(
                plan,
                disarmed,
                facts,
                prior_state=prior_state,
                blockers=blockers,
            )

        completed = facts.completed_one_minute_bar_utc
        if completed is None or completed == synchronized.last_completed_one_minute_bar_utc:
            return self._decision(
                plan,
                synchronized,
                facts,
                prior_state=prior_state,
            )
        if (
            synchronized.last_completed_one_minute_bar_utc is not None
            and completed < synchronized.last_completed_one_minute_bar_utc
        ):
            return self._decision(
                plan,
                synchronized,
                facts,
                prior_state=prior_state,
                blockers=("bar_time_regressed",),
            )

        confirmations = synchronized.consecutive_confirmations + 1
        confirmed_runtime = replace(
            synchronized,
            consecutive_confirmations=confirmations,
            last_completed_one_minute_bar_utc=completed,
        )
        if confirmations == 1:
            return self._decision(
                plan,
                confirmed_runtime,
                facts,
                prior_state=prior_state,
                action=PlanAction.ARM_ENTRY,
                next_state=PlanState.ARMED,
                material=False,
                reasons=("first_completed_minute_confirmation",),
            )
        if synchronized.state is PlanState.ENTRY_READY:
            return self._decision(
                plan,
                confirmed_runtime,
                facts,
                prior_state=prior_state,
                next_state=PlanState.ENTRY_READY,
            )
        soft_blocker = self._soft_revision_blocker(plan, synchronized, facts)
        if soft_blocker is not None:
            return self._decision(
                plan,
                confirmed_runtime,
                facts,
                prior_state=prior_state,
                next_state=PlanState.ARMED,
                blockers=(soft_blocker,),
            )
        return self._decision(
            plan,
            confirmed_runtime,
            facts,
            prior_state=prior_state,
            action=PlanAction.ENTER_PROBE,
            next_state=PlanState.ENTRY_READY,
            material=True,
            reasons=("two_completed_minute_confirmations", "entry_confluence_passed"),
            soft_revision=True,
            suggested_shares=self._probe_size(plan, entry_capacity),
        )

    @staticmethod
    def _risk_capacity(
        plan: BaselineTradePlan,
        facts: RealtimePlanFacts,
        *,
        position: PositionFacts | None,
        effective_stop: float,
    ) -> int:
        risk_per_new_share = facts.ask - effective_stop
        if risk_per_new_share <= 0:
            return 0
        current_notional = 0.0 if position is None else position.shares * facts.ask
        current_risk = (
            0.0
            if position is None
            else position.shares * max(position.average_entry - effective_stop, 0.0)
        )
        remaining_notional = plan.max_notional - current_notional
        remaining_risk = plan.max_risk_dollars - current_risk
        if remaining_notional <= 0 or remaining_risk <= 0:
            return 0
        by_notional = math.floor(remaining_notional / facts.ask)
        by_risk = math.floor(remaining_risk / risk_per_new_share)
        return max(0, min(by_notional, by_risk))

    @staticmethod
    def _probe_size(plan: BaselineTradePlan, capacity: int) -> int:
        if capacity < 1:
            raise ValueError("probe size requires positive risk capacity")
        return max(1, math.floor(capacity * plan.probe_fraction))

    def _quote_blockers(
        self,
        plan: BaselineTradePlan,
        facts: RealtimePlanFacts,
    ) -> tuple[str, ...]:
        blockers: list[str] = []
        if not facts.data_complete:
            blockers.append("market_data_incomplete")
        if facts.quote_age_seconds > self._MAX_QUOTE_AGE_SECONDS:
            blockers.append("quote_stale")
        if facts.spread_ratio > plan.max_spread_ratio:
            blockers.append("spread_too_wide")
        return tuple(blockers)

    def _entry_blockers(
        self,
        plan: BaselineTradePlan,
        facts: RealtimePlanFacts,
    ) -> tuple[str, ...]:
        blockers: list[str] = []
        if facts.market_risk_off:
            blockers.append("market_risk_off")
        if not facts.benchmark_above_vwap:
            blockers.append("benchmark_below_vwap")
        if not facts.sector_above_vwap:
            blockers.append("sector_below_vwap")
        if facts.session_vwap is None or facts.last_price <= facts.session_vwap:
            blockers.append("below_session_vwap")
        if not facts.fifteen_minute_confirmed:
            blockers.append("fifteen_minute_trend_not_confirmed")
        if not facts.five_minute_confirmed:
            blockers.append("five_minute_structure_not_confirmed")
        if not facts.one_minute_trigger:
            blockers.append("one_minute_trigger_not_confirmed")
        if (
            facts.green_volume_ratio is None
            or facts.green_volume_ratio < self._MIN_GREEN_VOLUME_RATIO
        ):
            blockers.append("green_volume_not_confirmed")
        if (
            facts.relative_strength is None
            or facts.relative_strength <= self._MIN_RELATIVE_STRENGTH
        ):
            blockers.append("relative_strength_not_confirmed")
        if plan.mode is PlanMode.CATALYST:
            if facts.catalyst_score is None:
                blockers.append("catalyst_unavailable")
            elif facts.catalyst_score < self._MIN_CATALYST_SCORE:
                blockers.append("catalyst_below_threshold")
        else:
            if facts.order_flow_imbalance is None:
                blockers.append("order_flow_unavailable")
            elif facts.order_flow_imbalance < self._MIN_ORDER_FLOW_IMBALANCE:
                blockers.append("order_flow_below_threshold")
        return tuple(blockers)

    @staticmethod
    def _soft_revision_blocker(
        plan: BaselineTradePlan,
        runtime: PlanRuntime,
        facts: RealtimePlanFacts,
    ) -> str | None:
        if runtime.soft_revision_count >= plan.max_soft_revisions:
            return "soft_revision_limit_reached"
        if (
            runtime.last_material_revision_utc is not None
            and facts.observed_at_utc - runtime.last_material_revision_utc
            < plan.soft_cooldown
        ):
            return "soft_revision_cooldown_active"
        return None

    @staticmethod
    def _decision(
        plan: BaselineTradePlan,
        runtime: PlanRuntime,
        facts: RealtimePlanFacts,
        *,
        prior_state: PlanState,
        action: PlanAction = PlanAction.NO_ACTION,
        next_state: PlanState | None = None,
        material: bool = False,
        reasons: tuple[str, ...] = (),
        blockers: tuple[str, ...] = (),
        soft_revision: bool = False,
        risk_revision: bool = False,
        suggested_shares: int | None = None,
    ) -> PlanDecision:
        target_state = next_state or runtime.state
        updated = replace(runtime, state=target_state)
        if material:
            updated = replace(
                updated,
                revision=updated.revision + 1,
                last_material_revision_utc=facts.observed_at_utc,
                soft_revision_count=(
                    updated.soft_revision_count + 1
                    if soft_revision
                    else updated.soft_revision_count
                ),
            )
        if soft_revision and risk_revision:
            raise AssertionError("a revision cannot be both soft and risk-driven")
        return PlanDecision(
            plan_id=plan.plan_id,
            symbol=plan.symbol,
            observed_at_utc=facts.observed_at_utc,
            action=action,
            prior_state=prior_state,
            next_state=target_state,
            material_revision=material,
            reasons=reasons,
            blockers=blockers,
            runtime=updated,
            suggested_shares=suggested_shares,
        )
