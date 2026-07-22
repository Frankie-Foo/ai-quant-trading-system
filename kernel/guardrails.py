"""The single fail-closed P0 -> P1 -> P2 trade arbitration sequence."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kernel.config import Config
from kernel.tradeplan import TradePlan


class RiskPriority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class RiskCode(StrEnum):
    P0_KILL_SWITCH = "P0_KILL_SWITCH"
    P0_NOT_PAPER = "P0_NOT_PAPER"
    P0_ACCOUNT = "P0_ACCOUNT"
    P0_TRADING_BLOCKED = "P0_TRADING_BLOCKED"
    P0_MARKET_CLOSED = "P0_MARKET_CLOSED"
    P0_MARKET_DATA = "P0_MARKET_DATA"
    P1_DAILY_LOSS = "P1_DAILY_LOSS"
    P1_MAX_POSITIONS = "P1_MAX_POSITIONS"
    P1_GROSS_EXPOSURE = "P1_GROSS_EXPOSURE"
    P1_BUYING_POWER = "P1_BUYING_POWER"
    P2_NOT_SELECTED = "P2_NOT_SELECTED"
    P2_EVIDENCE = "P2_EVIDENCE"
    P2_SIZING_CAP = "P2_SIZING_CAP"


class GuardrailContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluated_at_utc: datetime
    market_data_asof_utc: datetime
    market_data_feed: str = Field(min_length=1)
    paper_endpoint: bool
    kill_switch_active: bool
    market_open: bool
    account_active: bool
    account_blocked: bool
    trading_blocked: bool
    equity: Decimal = Field(gt=0)
    daily_pnl: Decimal
    gross_exposure: Decimal = Field(ge=0)
    open_position_symbols: tuple[str, ...] = ()
    buying_power: Decimal = Field(ge=0)
    sizing_notional_cap: Decimal = Field(gt=0)
    selected_symbols: tuple[str, ...] = Field(min_length=1)
    selection_snapshot_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("evaluated_at_utc", "market_data_asof_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("guardrail timestamps must be timezone-aware UTC")
        return value

    @field_validator("open_position_symbols", "selected_symbols", mode="before")
    @classmethod
    def normalize_symbols(cls, value: object) -> object:
        if isinstance(value, (tuple, list)):
            return tuple(str(item).strip().upper() for item in value)
        return value

    @field_validator("equity", "daily_pnl", "gross_exposure", "buying_power", "sizing_notional_cap")
    @classmethod
    def finite_decimal(cls, value: Decimal) -> Decimal:
        if not math.isfinite(float(value)):
            raise ValueError("guardrail numeric inputs must be finite")
        return value

    @model_validator(mode="after")
    def market_data_not_from_future(self) -> GuardrailContext:
        if self.market_data_asof_utc > self.evaluated_at_utc:
            raise ValueError("market data timestamp cannot be in the future")
        return self


class GuardrailCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    priority: RiskPriority
    code: RiskCode
    passed: bool
    observed: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    provenance: str = Field(min_length=1)


class GuardrailVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approved: bool
    failure_code: RiskCode | None
    checks: tuple[GuardrailCheck, ...]
    provenance: str = "kernel.guardrails.arbitrate_trade.v1"


def _check(
    priority: RiskPriority,
    code: RiskCode,
    passed: bool,
    observed: str,
    expected: str,
) -> GuardrailCheck:
    return GuardrailCheck(
        priority=priority,
        code=code,
        passed=passed,
        observed=observed,
        expected=expected,
        provenance=f"kernel.guardrails.{code.value}.v1",
    )


def arbitrate_trade(plan: TradePlan, context: GuardrailContext, cfg: Config) -> GuardrailVerdict:
    """Evaluate one ordered sequence and return immediately on the first failure."""

    freshness_seconds = (context.evaluated_at_utc - context.market_data_asof_utc).total_seconds()
    max_gross = context.equity * Decimal(str(cfg.max_gross_exposure))
    daily_loss_floor = -(context.equity * Decimal(str(cfg.guardrails.daily_loss_limit)))
    plan_notional = plan.reference_notional
    symbol_already_open = plan.symbol in context.open_position_symbols
    position_capacity = (
        symbol_already_open or len(context.open_position_symbols) < cfg.max_concurrent
    )
    evidence_bound = bool(
        set(context.selection_snapshot_ids).intersection(plan.source_snapshot_ids)
    )

    candidates = (
        _check(
            RiskPriority.P0,
            RiskCode.P0_KILL_SWITCH,
            not context.kill_switch_active,
            str(context.kill_switch_active).lower(),
            "false",
        ),
        _check(
            RiskPriority.P0,
            RiskCode.P0_NOT_PAPER,
            context.paper_endpoint,
            str(context.paper_endpoint).lower(),
            "true",
        ),
        _check(
            RiskPriority.P0,
            RiskCode.P0_ACCOUNT,
            context.account_active and not context.account_blocked,
            f"active={context.account_active},blocked={context.account_blocked}",
            "active=true,blocked=false",
        ),
        _check(
            RiskPriority.P0,
            RiskCode.P0_TRADING_BLOCKED,
            not context.trading_blocked,
            str(context.trading_blocked).lower(),
            "false",
        ),
        _check(
            RiskPriority.P0,
            RiskCode.P0_MARKET_CLOSED,
            context.market_open,
            str(context.market_open).lower(),
            "true",
        ),
        _check(
            RiskPriority.P0,
            RiskCode.P0_MARKET_DATA,
            context.market_data_feed.lower() == "sip"
            and freshness_seconds <= cfg.market_data.max_quote_age_seconds,
            f"feed={context.market_data_feed.lower()},age_seconds={freshness_seconds:.3f}",
            f"feed=sip,age_seconds<={cfg.market_data.max_quote_age_seconds:g}",
        ),
        _check(
            RiskPriority.P1,
            RiskCode.P1_DAILY_LOSS,
            context.daily_pnl > daily_loss_floor,
            str(context.daily_pnl),
            f">{daily_loss_floor}",
        ),
        _check(
            RiskPriority.P1,
            RiskCode.P1_MAX_POSITIONS,
            position_capacity,
            str(len(context.open_position_symbols)),
            f"<{cfg.max_concurrent} for a new symbol",
        ),
        _check(
            RiskPriority.P1,
            RiskCode.P1_GROSS_EXPOSURE,
            context.gross_exposure + plan_notional <= max_gross,
            str(context.gross_exposure + plan_notional),
            f"<={max_gross}",
        ),
        _check(
            RiskPriority.P1,
            RiskCode.P1_BUYING_POWER,
            plan_notional <= context.buying_power,
            str(plan_notional),
            f"<={context.buying_power}",
        ),
        _check(
            RiskPriority.P2,
            RiskCode.P2_NOT_SELECTED,
            plan.symbol in context.selected_symbols,
            plan.symbol,
            "symbol present in locked selection",
        ),
        _check(
            RiskPriority.P2,
            RiskCode.P2_EVIDENCE,
            evidence_bound,
            str(evidence_bound).lower(),
            "TradePlan references the locked selection snapshot",
        ),
        _check(
            RiskPriority.P2,
            RiskCode.P2_SIZING_CAP,
            plan_notional <= context.sizing_notional_cap,
            str(plan_notional),
            f"<={context.sizing_notional_cap}",
        ),
    )

    completed: list[GuardrailCheck] = []
    for candidate in candidates:
        completed.append(candidate)
        if not candidate.passed:
            return GuardrailVerdict(
                approved=False,
                failure_code=candidate.code,
                checks=tuple(completed),
            )
    return GuardrailVerdict(approved=True, failure_code=None, checks=tuple(completed))
