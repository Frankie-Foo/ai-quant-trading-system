"""Auditable, bracket-protected, permanently long-only trade intent."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TradePlan(BaseModel):
    """Broker-neutral intent produced only by the deterministic kernel."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    trace_id: str = Field(min_length=1, max_length=128)
    strategy_version: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Z][A-Z0-9.-]*$")
    trade_date: date
    decision_asof_utc: datetime
    created_at_utc: datetime
    side: Literal["buy"] = "buy"
    entry_order_type: Literal["market", "limit"] = "market"
    time_in_force: Literal["day"] = "day"
    extended_hours: Literal[False] = False
    quantity: int = Field(gt=0)
    reference_price: Decimal = Field(gt=0)
    entry_limit_price: Decimal | None = Field(default=None, gt=0)
    take_profit_price: Decimal = Field(gt=0)
    stop_loss_price: Decimal = Field(gt=0)
    time_stop_utc: datetime
    source_snapshot_ids: tuple[str, ...] = Field(min_length=1)
    provenance: str = Field(min_length=1)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("decision_asof_utc", "created_at_utc", "time_stop_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("TradePlan timestamps must be timezone-aware UTC")
        return value

    @field_validator("source_snapshot_ids")
    @classmethod
    def unique_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(item.strip() for item in value)
        if any(not item for item in cleaned):
            raise ValueError("source snapshot IDs cannot be empty")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("source snapshot IDs must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_intent(self) -> TradePlan:
        if self.decision_asof_utc > self.created_at_utc:
            raise ValueError("decision_asof_utc cannot be after plan creation")
        if self.time_stop_utc <= self.created_at_utc:
            raise ValueError("time stop must be after plan creation")
        if not self.stop_loss_price < self.reference_price < self.take_profit_price:
            raise ValueError("long bracket must satisfy stop < reference < take profit")
        if self.entry_order_type == "limit" and self.entry_limit_price is None:
            raise ValueError("limit entry requires entry_limit_price")
        if self.entry_order_type == "market" and self.entry_limit_price is not None:
            raise ValueError("market entry cannot include entry_limit_price")
        return self

    @property
    def reference_notional(self) -> Decimal:
        return self.reference_price * self.quantity

    @property
    def client_order_id(self) -> str:
        return f"tsv2-{self.plan_id}-entry"
