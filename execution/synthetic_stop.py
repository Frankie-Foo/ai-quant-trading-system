"""One-second deterministic synthetic stop for extended-hours long positions."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum


class StopAction(StrEnum):
    OBSERVE = "observe"
    SUBMIT_EXIT_LIMIT = "submit_exit_limit"
    CANCEL_REPLACE_EXIT = "cancel_replace_exit"
    COMPLETE = "complete"
    ALERT = "alert"


@dataclass(frozen=True)
class SyntheticStopPlan:
    plan_id: str
    symbol: str
    qty: int
    stop_price: Decimal
    confirmation_seconds: float = 2.0
    reprice_seconds: float = 2.0
    price_buffers: tuple[Decimal, ...] = (
        Decimal("0.0025"),
        Decimal("0.0050"),
        Decimal("0.0100"),
    )

    def __post_init__(self) -> None:
        if not self.plan_id.strip():
            raise ValueError("synthetic stop plan_id is required")
        if self.symbol != self.symbol.strip().upper() or not self.symbol:
            raise ValueError("synthetic stop symbol must be normalized uppercase")
        if self.qty <= 0:
            raise ValueError("synthetic stop quantity must be positive")
        if not self.stop_price.is_finite() or self.stop_price <= 0:
            raise ValueError("synthetic stop price must be finite and positive")
        if (
            not math.isfinite(self.confirmation_seconds)
            or self.confirmation_seconds <= 0
            or not math.isfinite(self.reprice_seconds)
            or self.reprice_seconds <= 0
        ):
            raise ValueError("synthetic stop timing must be finite and positive")
        if not self.price_buffers or any(
            not value.is_finite() or value <= 0 or value > Decimal("0.05")
            for value in self.price_buffers
        ):
            raise ValueError("synthetic stop price buffers are invalid")


@dataclass(frozen=True)
class SyntheticStopRuntime:
    below_since_utc: datetime | None
    triggered_at_utc: datetime | None
    last_command_at_utc: datetime | None
    price_attempt: int
    completed_at_utc: datetime | None

    @classmethod
    def initial(cls) -> SyntheticStopRuntime:
        return cls(
            below_since_utc=None,
            triggered_at_utc=None,
            last_command_at_utc=None,
            price_attempt=0,
            completed_at_utc=None,
        )

    def __post_init__(self) -> None:
        for value in (
            self.below_since_utc,
            self.triggered_at_utc,
            self.last_command_at_utc,
            self.completed_at_utc,
        ):
            if value is not None:
                _require_utc(value)
        if self.price_attempt < 0:
            raise ValueError("synthetic stop price_attempt cannot be negative")


@dataclass(frozen=True)
class SyntheticStopSnapshot:
    observed_at_utc: datetime
    quote_asof_utc: datetime
    bid: Decimal
    ask: Decimal
    last_trade: Decimal | None
    last_trade_asof_utc: datetime | None
    quote_provenance: str
    trade_provenance: str | None
    data_healthy: bool
    broker_healthy: bool
    verified_material_negative: bool
    halt_risk: bool
    filled: bool

    def __post_init__(self) -> None:
        _require_utc(self.observed_at_utc)
        _require_utc(self.quote_asof_utc)
        if self.last_trade_asof_utc is not None:
            _require_utc(self.last_trade_asof_utc)
        if self.quote_asof_utc > self.observed_at_utc:
            raise ValueError("synthetic stop quote cannot be from the future")
        if (
            self.last_trade_asof_utc is not None
            and self.last_trade_asof_utc > self.observed_at_utc
        ):
            raise ValueError("synthetic stop trade cannot be from the future")
        if (
            not self.bid.is_finite()
            or not self.ask.is_finite()
            or self.bid <= 0
            or self.ask < self.bid
        ):
            raise ValueError("synthetic stop NBBO is invalid")
        if self.last_trade is not None and (
            not self.last_trade.is_finite() or self.last_trade <= 0
        ):
            raise ValueError("synthetic stop last trade is invalid")
        if not self.quote_provenance.strip():
            raise ValueError("synthetic stop quote provenance is required")


@dataclass(frozen=True)
class SyntheticStopDecision:
    action: StopAction
    runtime: SyntheticStopRuntime
    limit_price: Decimal | None
    reasons: tuple[str, ...]
    blockers: tuple[str, ...]


class SyntheticStopEngine:
    """Return the next stop command without owning broker or storage side effects."""

    def evaluate(
        self,
        plan: SyntheticStopPlan,
        runtime: SyntheticStopRuntime,
        snapshot: SyntheticStopSnapshot,
    ) -> SyntheticStopDecision:
        if snapshot.filled or runtime.completed_at_utc is not None:
            completed = (
                runtime
                if runtime.completed_at_utc is not None
                else replace(runtime, completed_at_utc=snapshot.observed_at_utc)
            )
            return SyntheticStopDecision(
                action=StopAction.COMPLETE,
                runtime=completed,
                limit_price=None,
                reasons=("synthetic_stop_filled",),
                blockers=(),
            )
        if runtime.triggered_at_utc is None:
            if not snapshot.data_healthy:
                most_aggressive_attempt = len(plan.price_buffers) - 1
                triggered = replace(
                    runtime,
                    below_since_utc=snapshot.observed_at_utc,
                    triggered_at_utc=snapshot.observed_at_utc,
                    last_command_at_utc=(
                        snapshot.observed_at_utc
                        if snapshot.broker_healthy
                        else None
                    ),
                    price_attempt=most_aggressive_attempt,
                )
                if not snapshot.broker_healthy:
                    return SyntheticStopDecision(
                        action=StopAction.ALERT,
                        runtime=triggered,
                        limit_price=None,
                        reasons=("market_data_unhealthy",),
                        blockers=("broker_unavailable_during_exit",),
                    )
                return SyntheticStopDecision(
                    action=StopAction.SUBMIT_EXIT_LIMIT,
                    runtime=triggered,
                    limit_price=_marketable_sell_limit(
                        snapshot.bid,
                        plan.price_buffers[most_aggressive_attempt],
                    ),
                    reasons=("market_data_unhealthy",),
                    blockers=(),
                )
            emergency_reason = (
                "verified_material_negative"
                if snapshot.verified_material_negative
                else "halt_risk"
                if snapshot.halt_risk
                else None
            )
            if emergency_reason is not None:
                triggered = replace(
                    runtime,
                    below_since_utc=snapshot.observed_at_utc,
                    triggered_at_utc=snapshot.observed_at_utc,
                    last_command_at_utc=(
                        snapshot.observed_at_utc
                        if snapshot.broker_healthy
                        else None
                    ),
                )
                if not snapshot.broker_healthy:
                    return SyntheticStopDecision(
                        action=StopAction.ALERT,
                        runtime=triggered,
                        limit_price=None,
                        reasons=(emergency_reason,),
                        blockers=("broker_unavailable_during_exit",),
                    )
                return SyntheticStopDecision(
                    action=StopAction.SUBMIT_EXIT_LIMIT,
                    runtime=triggered,
                    limit_price=_marketable_sell_limit(
                        snapshot.bid,
                        plan.price_buffers[0],
                    ),
                    reasons=(emergency_reason,),
                    blockers=(),
                )
            if (
                snapshot.last_trade is not None
                and snapshot.last_trade <= plan.stop_price
                and snapshot.bid <= plan.stop_price
            ):
                triggered = replace(
                    runtime,
                    below_since_utc=snapshot.observed_at_utc,
                    triggered_at_utc=snapshot.observed_at_utc,
                    last_command_at_utc=snapshot.observed_at_utc,
                )
                return SyntheticStopDecision(
                    action=StopAction.SUBMIT_EXIT_LIMIT,
                    runtime=triggered,
                    limit_price=_marketable_sell_limit(
                        snapshot.bid,
                        plan.price_buffers[0],
                    ),
                    reasons=("trade_and_nbbo_confirmed_stop",),
                    blockers=(),
                )
            if snapshot.bid > plan.stop_price:
                return SyntheticStopDecision(
                    action=StopAction.OBSERVE,
                    runtime=replace(runtime, below_since_utc=None),
                    limit_price=None,
                    reasons=(),
                    blockers=(),
                )
            below_since = runtime.below_since_utc or snapshot.observed_at_utc
            waiting = replace(runtime, below_since_utc=below_since)
            elapsed = (snapshot.observed_at_utc - below_since).total_seconds()
            if elapsed < plan.confirmation_seconds:
                return SyntheticStopDecision(
                    action=StopAction.OBSERVE,
                    runtime=waiting,
                    limit_price=None,
                    reasons=(),
                    blockers=("awaiting_two_second_confirmation",),
                )
            triggered = replace(
                waiting,
                triggered_at_utc=snapshot.observed_at_utc,
                last_command_at_utc=snapshot.observed_at_utc,
            )
            return SyntheticStopDecision(
                action=StopAction.SUBMIT_EXIT_LIMIT,
                runtime=triggered,
                limit_price=_marketable_sell_limit(
                    snapshot.bid,
                    plan.price_buffers[0],
                ),
                reasons=("nbbo_below_stop_for_two_seconds",),
                blockers=(),
            )
        if not snapshot.broker_healthy:
            return SyntheticStopDecision(
                action=StopAction.ALERT,
                runtime=runtime,
                limit_price=None,
                reasons=("synthetic_stop_triggered",),
                blockers=("broker_unavailable_during_exit",),
            )
        if runtime.last_command_at_utc is None:
            resumed = replace(
                runtime,
                last_command_at_utc=snapshot.observed_at_utc,
            )
            return SyntheticStopDecision(
                action=StopAction.SUBMIT_EXIT_LIMIT,
                runtime=resumed,
                limit_price=_marketable_sell_limit(
                    snapshot.bid,
                    plan.price_buffers[runtime.price_attempt],
                ),
                reasons=("broker_recovered_exit_retry",),
                blockers=(),
            )
        elapsed = (
            snapshot.observed_at_utc - runtime.last_command_at_utc
        ).total_seconds()
        if elapsed >= plan.reprice_seconds:
            attempt = min(runtime.price_attempt + 1, len(plan.price_buffers) - 1)
            updated = replace(
                runtime,
                last_command_at_utc=snapshot.observed_at_utc,
                price_attempt=attempt,
            )
            return SyntheticStopDecision(
                action=StopAction.CANCEL_REPLACE_EXIT,
                runtime=updated,
                limit_price=_marketable_sell_limit(
                    snapshot.bid,
                    plan.price_buffers[attempt],
                ),
                reasons=("synthetic_stop_reprice",),
                blockers=(),
            )
        return SyntheticStopDecision(
            action=StopAction.OBSERVE,
            runtime=runtime,
            limit_price=None,
            reasons=(),
            blockers=("exit_order_pending",),
        )


def _marketable_sell_limit(bid: Decimal, buffer: Decimal) -> Decimal:
    return (bid * (Decimal(1) - buffer)).quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN,
    )


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("synthetic stop timestamps must be timezone-aware UTC")
