from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OrderState(StrEnum):
    CREATED = "created"
    PENDING_RISK = "pending_risk"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


ALLOWED_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.CREATED: frozenset({OrderState.PENDING_RISK, OrderState.REJECTED}),
    OrderState.PENDING_RISK: frozenset({OrderState.APPROVED, OrderState.REJECTED}),
    OrderState.APPROVED: frozenset(
        {OrderState.SUBMITTED, OrderState.CANCELLED, OrderState.REJECTED}
    ),
    OrderState.SUBMITTED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_PENDING,
            OrderState.CANCELLED,
            OrderState.REJECTED,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_PENDING,
            OrderState.CANCELLED,
        }
    ),
    OrderState.CANCEL_PENDING: frozenset(
        {OrderState.CANCELLED, OrderState.PARTIALLY_FILLED, OrderState.FILLED}
    ),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.REJECTED: frozenset(),
}


class OrderEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    at_utc: datetime
    from_state: OrderState
    to_state: OrderState
    filled_shares: int = Field(ge=0)
    provenance: str = Field(min_length=1)

    @field_validator("at_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("order event timestamps must be timezone-aware")
        if value.utcoffset() != timedelta(0):
            raise ValueError("order event timestamps must be stored in UTC")
        return value


class OrderLifecycle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    client_order_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    requested_shares: int = Field(gt=0)
    state: OrderState = OrderState.CREATED
    filled_shares: int = Field(default=0, ge=0)
    events: tuple[OrderEvent, ...] = ()

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def validate_fills(self) -> OrderLifecycle:
        if self.filled_shares > self.requested_shares:
            raise ValueError("filled_shares cannot exceed requested_shares")
        if self.state is OrderState.FILLED and self.filled_shares != self.requested_shares:
            raise ValueError("filled order must equal requested_shares")
        return self


def apply_transition(
    order: OrderLifecycle,
    next_state: OrderState,
    *,
    at_utc: datetime,
    provenance: str,
    filled_shares: int | None = None,
) -> OrderLifecycle:
    if next_state not in ALLOWED_TRANSITIONS[order.state]:
        raise ValueError(f"illegal order transition: {order.state} -> {next_state}")

    new_filled = order.filled_shares if filled_shares is None else filled_shares
    if new_filled < order.filled_shares:
        raise ValueError("filled_shares cannot decrease")
    if next_state is OrderState.PARTIALLY_FILLED and not (
        order.filled_shares < new_filled < order.requested_shares
    ):
        raise ValueError("partial fill must increase and remain below requested_shares")
    if next_state is OrderState.FILLED and new_filled != order.requested_shares:
        raise ValueError("filled transition requires all requested shares")

    event = OrderEvent(
        sequence=len(order.events) + 1,
        at_utc=at_utc,
        from_state=order.state,
        to_state=next_state,
        filled_shares=new_filled,
        provenance=provenance,
    )
    payload = order.model_dump()
    payload.update(
        state=next_state,
        filled_shares=new_filled,
        events=(*order.events, event),
    )
    return OrderLifecycle.model_validate(payload)
