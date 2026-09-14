"""Standalone shadow candidate contract. Loading this policy never authorizes trading."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import ConfigDict, Field, TypeAdapter, field_validator, model_validator

from kernel.config import HH_MM_PATTERN, FrozenModel

_NONNEGATIVE: TypeAdapter[float] = TypeAdapter(
    Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
)
_EASTERN = ZoneInfo("America/New_York")


class _ValidatedFrozen(FrozenModel):
    model_config = ConfigDict(
        strict=True,
        allow_inf_nan=False,
        validate_default=True,
        revalidate_instances="always",
    )

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        # All fields are immutable scalars; either copy mode must validate updates.
        return type(self).model_validate({**self.model_dump(), **(update or {})})


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("market timestamp must be timezone-aware")
    return value.astimezone(UTC)


class MarketSession(_ValidatedFrozen):
    """Explicit row from data_plane.calendar, supplied by a trusted calendar adapter.

    Validates row consistency, not the authenticity of the source. No weekday or
    holiday inference is made in the kernel; a missing calendar row is no session.
    """

    trade_date: date
    market_open_utc: datetime
    market_close_utc: datetime
    session_minutes: int = Field(gt=0, le=390)
    is_half_day: bool
    source: str = Field(min_length=1)
    source_version: str = Field(min_length=1)

    @field_validator("market_open_utc", "market_close_utc")
    @classmethod
    def utc_timestamps(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @field_validator("source", "source_version")
    @classmethod
    def nonblank_source(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("calendar provenance must not be blank")
        return value

    @model_validator(mode="after")
    def consistent_session(self) -> Self:
        if (
            self.market_open_utc.astimezone(_EASTERN).date() != self.trade_date
            or self.market_close_utc.astimezone(_EASTERN).date() != self.trade_date
            or self.market_close_utc - self.market_open_utc
            != timedelta(minutes=self.session_minutes)
            or self.is_half_day != (self.session_minutes < 390)
        ):
            raise ValueError("inconsistent calendar session")
        return self


@dataclass(frozen=True)
class SessionWindow:
    entry_start_utc: datetime
    entry_cutoff_utc: datetime
    flatten_by_utc: datetime


@dataclass(frozen=True)
class RiskBudget:
    """Dollar ceilings, not a reservation ledger or permission to place orders."""

    risk_base_usd: float
    symbol_risk_usd: float
    first_attempt_usd: float
    second_attempt_usd: float
    symbol_notional_usd: float
    gross_notional_usd: float
    theme_risk_usd: float
    portfolio_risk_usd: float
    daily_loss_usd: float

    def available_attempt_risk(
        self,
        *,
        attempt: int,
        symbol_consumed_usd: float,
        theme_committed_usd: float,
        portfolio_committed_usd: float,
        daily_committed_usd: float,
    ) -> float:
        """Intersect remaining ceilings without crediting profits.

        Caller supplies nonnegative totals including losses, consumed/unbooked costs
        and position/pending-order risk for the relevant ceiling. Daily committed
        includes realized losses, current floating losses and remaining open risk
        without double counting. Persistent accounting and the latched daily-loss
        gate remain the caller's responsibility; a snapshot cannot reset either.
        """
        if type(attempt) is not int or attempt not in (1, 2):
            raise ValueError("attempt must be integer 1 or 2")
        return max(
            0.0,
            min(
                self.first_attempt_usd if attempt == 1 else self.second_attempt_usd,
                self.symbol_risk_usd - _NONNEGATIVE.validate_python(symbol_consumed_usd),
                self.theme_risk_usd - _NONNEGATIVE.validate_python(theme_committed_usd),
                self.portfolio_risk_usd - _NONNEGATIVE.validate_python(portfolio_committed_usd),
                self.daily_loss_usd - _NONNEGATIVE.validate_python(daily_committed_usd),
            ),
        )


class EventPolicy(_ValidatedFrozen):
    """v1 candidate only; promotion and approval verification belong to a later gate."""

    schema_version: Literal["event_intraday_policy.v1"]
    policy_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$")
    status: Literal["shadow"] = "shadow"
    capital_cap_usd: float = Field(gt=0, le=200_000)
    symbol_risk_fraction: float = Field(gt=0, le=0.005)
    first_attempt_fraction: float = Field(gt=0, le=0.6)
    second_attempt_fraction: float = Field(gt=0, le=0.4)
    symbol_notional_fraction: float = Field(gt=0, le=0.35)
    gross_notional_fraction: float = Field(gt=0, le=1)
    theme_risk_fraction: float = Field(gt=0, le=0.0075)
    portfolio_risk_fraction: float = Field(gt=0, le=0.015)
    daily_loss_fraction: float = Field(gt=0, le=0.015)
    max_positions: int = Field(gt=0, le=3)
    max_spread_fraction: float = Field(gt=0, le=0.0025)
    max_quote_age_seconds: float = Field(gt=0, le=2)
    cost_reserve_fraction: float = Field(ge=0.005, lt=0.02)
    max_all_in_stop_fraction: float = Field(gt=0, le=0.02)
    participation_fraction: float = Field(gt=0, le=0.01)
    market_timezone: Literal["America/New_York"]
    entry_start_et: str = Field(pattern=HH_MM_PATTERN)
    entry_cutoff_et: str = Field(pattern=HH_MM_PATTERN)
    flatten_by_et: str = Field(pattern=HH_MM_PATTERN)

    @field_validator("entry_start_et", "entry_cutoff_et", "flatten_by_et")
    @classmethod
    def ascii_clock(cls, value: str) -> str:
        if not value.isascii():
            raise ValueError("market clocks must use ASCII HH:MM")
        return value

    @model_validator(mode="after")
    def consistent_limits(self) -> Self:
        if self.first_attempt_fraction + self.second_attempt_fraction != 1:
            raise ValueError("attempt fractions must sum to one")
        if (
            not self.symbol_risk_fraction
            <= self.theme_risk_fraction
            <= self.portfolio_risk_fraction
        ):
            raise ValueError("risk limits must satisfy symbol <= theme <= portfolio")
        if self.symbol_notional_fraction > self.gross_notional_fraction:
            raise ValueError("symbol notional must not exceed gross notional")
        if self.cost_reserve_fraction >= self.max_all_in_stop_fraction:
            raise ValueError("all-in stop must leave room for structural risk after costs")
        if not (
            "10:00" <= self.entry_start_et < self.entry_cutoff_et <= "15:00"
            and self.entry_cutoff_et < self.flatten_by_et <= "15:50"
        ):
            raise ValueError(
                "market clocks must satisfy 10:00 <= start < cutoff <= 15:00"
                " and cutoff < flatten <= 15:50"
            )
        return self

    @property
    def policy_hash(self) -> str:
        """SHA-256 of validated, default-expanded JSON (sorted keys, compact UTF-8)."""
        encoded = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def validate_references(self, module_hashes: Mapping[str, str]) -> None:
        """Check caller-supplied module references, not approval or a digital signature.

        The integrating caller must supply every required module; this standalone
        candidate cannot discover consumers or enable/disable an order writer.
        """
        if not module_hashes:
            raise ValueError("policy hash references must not be empty")
        for module, reference in module_hashes.items():
            if not isinstance(module, str) or not module.strip() or reference != self.policy_hash:
                raise ValueError(f"policy hash mismatch or missing source: {module!r}")

    def risk_budget(self, *, opening_equity_usd: float, current_equity_usd: float) -> RiskBudget:
        """Use equity, never buying power. Recompute with current equity before a decision."""
        base = min(
            self.capital_cap_usd,
            _NONNEGATIVE.validate_python(opening_equity_usd),
            _NONNEGATIVE.validate_python(current_equity_usd),
        )
        symbol = base * self.symbol_risk_fraction
        return RiskBudget(
            risk_base_usd=base,
            symbol_risk_usd=symbol,
            first_attempt_usd=symbol * self.first_attempt_fraction,
            second_attempt_usd=symbol * self.second_attempt_fraction,
            symbol_notional_usd=base * self.symbol_notional_fraction,
            gross_notional_usd=base * self.gross_notional_fraction,
            theme_risk_usd=base * self.theme_risk_fraction,
            portfolio_risk_usd=base * self.portfolio_risk_fraction,
            daily_loss_usd=base * self.daily_loss_fraction,
        )

    def session_window(self, session: MarketSession | None) -> SessionWindow:
        """v1: entry cutoff <= close-60m; flatten <= close-10m, also on short days."""
        if not isinstance(session, MarketSession):
            raise ValueError("explicit calendar session is required")

        def at(clock: str) -> datetime:
            return datetime.combine(
                session.trade_date, time.fromisoformat(clock), _EASTERN
            ).astimezone(UTC)

        start = max(at(self.entry_start_et), session.market_open_utc)
        cutoff = min(at(self.entry_cutoff_et), session.market_close_utc - timedelta(minutes=60))
        flatten = min(at(self.flatten_by_et), session.market_close_utc - timedelta(minutes=10))
        # An empty entry interval must not suppress the independent flatten deadline.
        return SessionWindow(start, cutoff, flatten)

    def entry_allowed_at(self, now: datetime, session: MarketSession | None) -> bool:
        """Time gate only; True is not approval, a signal, or broker authorization."""
        current = _aware_utc(now)
        if session is None:
            return False
        window = self.session_window(session)
        if current.astimezone(_EASTERN).date() != session.trade_date:
            raise ValueError("timestamp does not belong to calendar session")
        return window.entry_start_utc <= current < window.entry_cutoff_utc

    def must_flatten_at(self, now: datetime, session: MarketSession | None) -> bool:
        """Deadline signal only. Missing/stale session raises; never assume flat."""
        current = _aware_utc(now)
        if session is None:
            raise ValueError("explicit calendar session is required")
        window = self.session_window(session)
        if current.astimezone(_EASTERN).date() != session.trade_date:
            raise ValueError("timestamp does not belong to calendar session")
        return current >= window.flatten_by_utc


def load_event_policy(path: str | Path, *, expected_hash: str | None = None) -> EventPolicy:
    """Read only the explicitly supplied candidate file, never active-policy env vars."""

    def unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        payload: dict[str, object] = {}
        for key, value in pairs:
            if key in payload:
                raise ValueError(f"duplicate policy key: {key}")
            payload[key] = value
        return payload

    payload = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
    policy = EventPolicy.model_validate(payload)
    if expected_hash is not None:
        policy.validate_references({"candidate file": expected_hash})
    return policy
