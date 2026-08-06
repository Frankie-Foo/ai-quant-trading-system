"""Deterministic ten-second lifecycle for premarket probe limit orders."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from enum import StrEnum


class PremarketEntryAction(StrEnum):
    OBSERVE = "observe"
    SUBMIT_LIMIT = "submit_limit"
    CANCEL_REPLACE = "cancel_replace"
    CANCEL_REMAINDER = "cancel_remainder"
    COMPLETE = "complete"
    ABANDON = "abandon"


@dataclass(frozen=True)
class PremarketEntryPlan:
    plan_id: str
    symbol: str
    target_qty: int
    reference_price: Decimal
    reprice_seconds: float = 3.0
    total_ttl_seconds: float = 10.0
    max_reprices: int = 2
    max_chase_fraction: Decimal = Decimal("0.003")

    def __post_init__(self) -> None:
        if not self.plan_id.strip():
            raise ValueError("premarket entry plan_id is required")
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("premarket entry symbol must be normalized uppercase")
        if self.target_qty <= 0:
            raise ValueError("premarket entry target quantity must be positive")
        if not self.reference_price.is_finite() or self.reference_price <= 0:
            raise ValueError("premarket entry reference price is invalid")
        if (
            not math.isfinite(self.reprice_seconds)
            or self.reprice_seconds <= 0
            or not math.isfinite(self.total_ttl_seconds)
            or self.total_ttl_seconds <= self.reprice_seconds
        ):
            raise ValueError("premarket entry timing is invalid")
        if self.max_reprices < 0:
            raise ValueError("premarket entry max reprices cannot be negative")
        if (
            not self.max_chase_fraction.is_finite()
            or self.max_chase_fraction < 0
            or self.max_chase_fraction > Decimal("0.02")
        ):
            raise ValueError("premarket entry chase cap is invalid")


@dataclass(frozen=True)
class PremarketEntryRuntime:
    started_at_utc: datetime | None
    last_command_at_utc: datetime | None
    attempt: int
    completed_at_utc: datetime | None

    @classmethod
    def initial(cls) -> PremarketEntryRuntime:
        return cls(
            started_at_utc=None,
            last_command_at_utc=None,
            attempt=0,
            completed_at_utc=None,
        )

    def __post_init__(self) -> None:
        for value in (
            self.started_at_utc,
            self.last_command_at_utc,
            self.completed_at_utc,
        ):
            if value is not None:
                _require_utc(value)
        if self.attempt < 0:
            raise ValueError("premarket entry attempt cannot be negative")


@dataclass(frozen=True)
class PremarketEntrySnapshot:
    observed_at_utc: datetime
    quote_asof_utc: datetime
    bid: Decimal
    ask: Decimal
    filled_qty: int
    order_working: bool
    quote_provenance: str
    data_healthy: bool
    broker_healthy: bool

    def __post_init__(self) -> None:
        _require_utc(self.observed_at_utc)
        _require_utc(self.quote_asof_utc)
        if self.quote_asof_utc > self.observed_at_utc:
            raise ValueError("premarket entry quote cannot be from the future")
        if (
            not self.bid.is_finite()
            or not self.ask.is_finite()
            or self.bid <= 0
            or self.ask < self.bid
        ):
            raise ValueError("premarket entry NBBO is invalid")
        if self.filled_qty < 0:
            raise ValueError("premarket entry filled quantity cannot be negative")
        if not self.quote_provenance.strip():
            raise ValueError("premarket entry quote provenance is required")


@dataclass(frozen=True)
class PremarketEntryDecision:
    action: PremarketEntryAction
    runtime: PremarketEntryRuntime
    limit_price: Decimal | None
    remaining_qty: int
    protection_required_qty: int
    reasons: tuple[str, ...]
    blockers: tuple[str, ...]


class PremarketEntryEngine:
    def evaluate(
        self,
        plan: PremarketEntryPlan,
        runtime: PremarketEntryRuntime,
        snapshot: PremarketEntrySnapshot,
    ) -> PremarketEntryDecision:
        filled = min(snapshot.filled_qty, plan.target_qty)
        remaining = plan.target_qty - filled
        if remaining == 0 or runtime.completed_at_utc is not None:
            completed = (
                runtime
                if runtime.completed_at_utc is not None
                else replace(runtime, completed_at_utc=snapshot.observed_at_utc)
            )
            return PremarketEntryDecision(
                action=PremarketEntryAction.COMPLETE,
                runtime=completed,
                limit_price=None,
                remaining_qty=0,
                protection_required_qty=filled,
                reasons=("premarket_target_filled",),
                blockers=(),
            )
        if not snapshot.data_healthy or not snapshot.broker_healthy:
            blocker = (
                "market_data_unhealthy"
                if not snapshot.data_healthy
                else "broker_unhealthy"
            )
            action = (
                PremarketEntryAction.CANCEL_REMAINDER
                if filled > 0
                else PremarketEntryAction.ABANDON
            )
            return PremarketEntryDecision(
                action=action,
                runtime=replace(runtime, completed_at_utc=snapshot.observed_at_utc),
                limit_price=None,
                remaining_qty=remaining,
                protection_required_qty=filled,
                reasons=("entry_fault_fail_closed",),
                blockers=(blocker,),
            )
        if runtime.started_at_utc is None:
            started = replace(
                runtime,
                started_at_utc=snapshot.observed_at_utc,
                last_command_at_utc=snapshot.observed_at_utc,
            )
            return PremarketEntryDecision(
                action=PremarketEntryAction.SUBMIT_LIMIT,
                runtime=started,
                limit_price=_bounded_buy_limit(plan, snapshot.ask),
                remaining_qty=remaining,
                protection_required_qty=0,
                reasons=("premarket_probe_started",),
                blockers=(),
            )
        elapsed_total = (
            snapshot.observed_at_utc - runtime.started_at_utc
        ).total_seconds()
        if elapsed_total >= plan.total_ttl_seconds:
            completed = replace(runtime, completed_at_utc=snapshot.observed_at_utc)
            return PremarketEntryDecision(
                action=(
                    PremarketEntryAction.CANCEL_REMAINDER
                    if filled > 0
                    else PremarketEntryAction.ABANDON
                ),
                runtime=completed,
                limit_price=None,
                remaining_qty=remaining,
                protection_required_qty=filled,
                reasons=(
                    ("entry_ttl_partial_fill",)
                    if filled > 0
                    else ("entry_ttl_unfilled",)
                ),
                blockers=(),
            )
        if runtime.last_command_at_utc is None:
            raise RuntimeError("started premarket entry lacks last command time")
        elapsed_command = (
            snapshot.observed_at_utc - runtime.last_command_at_utc
        ).total_seconds()
        if (
            snapshot.order_working
            and runtime.attempt < plan.max_reprices
            and elapsed_command >= plan.reprice_seconds
        ):
            updated = replace(
                runtime,
                attempt=runtime.attempt + 1,
                last_command_at_utc=snapshot.observed_at_utc,
            )
            return PremarketEntryDecision(
                action=PremarketEntryAction.CANCEL_REPLACE,
                runtime=updated,
                limit_price=_bounded_buy_limit(plan, snapshot.ask),
                remaining_qty=remaining,
                protection_required_qty=filled,
                reasons=("premarket_limit_reprice",),
                blockers=(),
            )
        return PremarketEntryDecision(
            action=PremarketEntryAction.OBSERVE,
            runtime=runtime,
            limit_price=None,
            remaining_qty=remaining,
            protection_required_qty=filled,
            reasons=(),
            blockers=("entry_order_pending",),
        )


def _bounded_buy_limit(
    plan: PremarketEntryPlan,
    ask: Decimal,
) -> Decimal:
    cap = (
        plan.reference_price * (Decimal(1) + plan.max_chase_fraction)
    ).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    ask_cent = ask.quantize(Decimal("0.01"), rounding=ROUND_UP)
    return min(ask_cent, cap)


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("premarket entry timestamp must be UTC")
