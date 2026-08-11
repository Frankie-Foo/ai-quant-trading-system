"""Fail-closed adapter from causal SIP facts to the autonomous Paper policy."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from execution.alpaca_paper import PaperPosition
from execution.autonomous_paper_session import (
    AutonomousPaperPlan,
    PaperSessionSnapshot,
)
from kernel.adaptive_trade_plan import RealtimePlanFacts
from kernel.intraday_policy import (
    DecisionMetric,
    EntryRoute,
    PolicySnapshot,
)

SCHEMA_VERSION = "runtime_safety_envelope.v1"
NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class AutonomousPolicyEvidence:
    """Immutable, pre-trade evidence whose timestamps predate every decision."""

    route: EntryRoute
    catalyst: DecisionMetric
    factor: DecisionMetric
    right_tail: DecisionMetric
    first_target_reward_r: float
    weighted_expected_reward_r: float
    reward_risk_provenance: str
    a_plus_plus_approved: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("first_target_reward_r", self.first_target_reward_r),
            ("weighted_expected_reward_r", self.weighted_expected_reward_r),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not self.reward_risk_provenance.strip():
            raise ValueError("reward/risk provenance is required")


@dataclass(frozen=True)
class RuntimeSafetyEnvelope:
    """Short-lived health/news assertion refreshed outside the deterministic kernel."""

    trade_date: date
    symbol: str
    generated_at_utc: datetime
    expires_at_utc: datetime
    negative_news_clear: bool | None
    material_negative: bool
    agents_healthy: bool
    push_healthy: bool
    source_snapshot_ids: tuple[str, ...]
    provenance: str

    def __post_init__(self) -> None:
        _require_utc(self.generated_at_utc, name="generated_at_utc")
        _require_utc(self.expires_at_utc, name="expires_at_utc")
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("runtime safety symbol must be normalized uppercase")
        ttl = self.expires_at_utc - self.generated_at_utc
        if not timedelta(0) < ttl <= timedelta(minutes=5):
            raise ValueError("runtime safety TTL must be in (0, 5m]")
        if not self.source_snapshot_ids or any(
            not item.strip() for item in self.source_snapshot_ids
        ):
            raise ValueError("runtime safety source snapshot IDs are required")
        if not self.provenance.strip():
            raise ValueError("runtime safety provenance is required")

    def is_current(self, observed_at_utc: datetime) -> bool:
        _require_utc(observed_at_utc, name="observed_at_utc")
        if self.generated_at_utc > observed_at_utc:
            raise ValueError("runtime safety generated_at cannot be in the future")
        return observed_at_utc <= self.expires_at_utc


class AutonomousPolicySnapshotFactory:
    """Combine static evidence, dynamic SIP facts, and broker-authoritative state."""

    def build(
        self,
        *,
        plan: AutonomousPaperPlan,
        evidence: AutonomousPolicyEvidence,
        facts: RealtimePlanFacts,
        envelope: RuntimeSafetyEnvelope | None,
        position: PaperPosition | None,
        account_equity: Decimal,
        main_profit_realized: bool = False,
    ) -> PaperSessionSnapshot:
        observed_at = facts.observed_at_utc
        if observed_at.astimezone(NEW_YORK).date() != plan.trade_date:
            raise ValueError("market facts and autonomous plan trade dates do not match")
        for metric in (evidence.catalyst, evidence.factor, evidence.right_tail):
            if metric.asof_utc > observed_at:
                raise ValueError("static policy evidence cannot be from the future")

        current_envelope = self._current_envelope(
            plan=plan,
            observed_at_utc=observed_at,
            envelope=envelope,
        )
        position_qty, average_entry = _position_state(plan, position)
        full_quantity = _full_position_quantity(
            plan,
            equity=account_equity,
            entry_price=plan.reference_price,
        )
        position_fraction = (
            0.0
            if position_qty == 0
            else _canonical_position_fraction(
                position_qty=position_qty,
                full_quantity=full_quantity,
            )
        )
        quote_age = (observed_at - facts.quote_ts_utc).total_seconds()
        data_healthy = bool(facts.data_complete and 0 <= quote_age <= 30)
        technical_structure_valid = bool(
            facts.one_minute_trigger
            and facts.five_minute_confirmed
            and facts.fifteen_minute_confirmed
            and facts.session_vwap is not None
            and facts.last_price > facts.session_vwap
        )
        order_flow = DecisionMetric(
            value=facts.order_flow_confirmation_score,
            asof_utc=observed_at,
            provenance=(
                facts.order_flow_provenance or "kernel.order_flow_confirmation.unavailable"
            ),
        )
        execution = DecisionMetric(
            value=_execution_score(plan, facts) if data_healthy else None,
            asof_utc=facts.quote_ts_utc,
            provenance=(
                f"{facts.quote_provenance or 'quote.provenance.unavailable'}|spread_quality.v1"
            ),
        )
        policy = PolicySnapshot(
            trade_date=plan.trade_date,
            observed_at_utc=observed_at,
            route=evidence.route,
            catalyst=evidence.catalyst,
            factor=evidence.factor,
            order_flow=order_flow,
            execution=execution,
            right_tail=evidence.right_tail,
            technical_structure_valid=technical_structure_valid,
            negative_news_clear=(
                current_envelope.negative_news_clear if current_envelope is not None else None
            ),
            material_negative=(
                current_envelope.material_negative if current_envelope is not None else False
            ),
            data_healthy=data_healthy,
            agents_healthy=bool(current_envelope is not None and current_envelope.agents_healthy),
            push_healthy=bool(current_envelope is not None and current_envelope.push_healthy),
            has_position=position_qty > 0,
            position_fraction=position_fraction,
            average_entry_price=average_entry,
            last_price=facts.last_price,
            approved_account_risk_fraction=float(plan.full_risk_fraction),
            main_profit_realized=main_profit_realized,
            a_plus_plus_approved=evidence.a_plus_plus_approved,
            first_target_reward_r=evidence.first_target_reward_r,
            weighted_expected_reward_r=evidence.weighted_expected_reward_r,
            reward_risk_provenance=evidence.reward_risk_provenance,
        )
        return PaperSessionSnapshot(
            policy=policy,
            bid=Decimal(str(facts.bid)),
            ask=Decimal(str(facts.ask)),
            quote_asof_utc=facts.quote_ts_utc,
            quote_provenance=(facts.quote_provenance or "quote.provenance.unavailable"),
            below_anchored_vwap_5m_bars=(facts.below_anchored_vwap_5m_bars),
            failed_vwap_reclaim=facts.failed_vwap_reclaim,
            chandelier_stop_hit=facts.chandelier_stop_hit,
            tail_hard_breakdown=facts.tail_hard_breakdown,
        )

    @staticmethod
    def _current_envelope(
        *,
        plan: AutonomousPaperPlan,
        observed_at_utc: datetime,
        envelope: RuntimeSafetyEnvelope | None,
    ) -> RuntimeSafetyEnvelope | None:
        if envelope is None:
            return None
        if envelope.trade_date != plan.trade_date or envelope.symbol != plan.symbol:
            raise ValueError("runtime safety envelope identity does not match plan")
        return envelope if envelope.is_current(observed_at_utc) else None


def write_runtime_safety_envelope(
    path: Path,
    envelope: RuntimeSafetyEnvelope,
) -> None:
    """Write one envelope atomically so readers never observe partial JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "trade_date": envelope.trade_date.isoformat(),
        "symbol": envelope.symbol,
        "generated_at_utc": envelope.generated_at_utc.isoformat(),
        "expires_at_utc": envelope.expires_at_utc.isoformat(),
        "negative_news_clear": envelope.negative_news_clear,
        "material_negative": envelope.material_negative,
        "agents_healthy": envelope.agents_healthy,
        "push_healthy": envelope.push_healthy,
        "source_snapshot_ids": list(envelope.source_snapshot_ids),
        "provenance": envelope.provenance,
    }
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1)
    finally:
        temporary.unlink(missing_ok=True)


def load_runtime_safety_envelope(path: Path) -> RuntimeSafetyEnvelope:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("runtime safety envelope is unreadable") from exc
    if not isinstance(payload, dict):
        raise ValueError("runtime safety envelope root must be an object")
    values = cast(dict[str, Any], payload)
    expected = {
        "schema_version",
        "trade_date",
        "symbol",
        "generated_at_utc",
        "expires_at_utc",
        "negative_news_clear",
        "material_negative",
        "agents_healthy",
        "push_healthy",
        "source_snapshot_ids",
        "provenance",
    }
    unexpected = set(values) - expected
    missing = expected - set(values)
    if unexpected:
        raise ValueError("runtime safety envelope has unexpected fields")
    if missing:
        raise ValueError("runtime safety envelope is missing required fields")
    if values["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported runtime safety envelope schema")
    source_ids = values["source_snapshot_ids"]
    if not isinstance(source_ids, list) or not all(isinstance(item, str) for item in source_ids):
        raise ValueError("runtime safety source_snapshot_ids must be strings")
    negative_news_clear = values["negative_news_clear"]
    if negative_news_clear is not None and not isinstance(negative_news_clear, bool):
        raise ValueError("negative_news_clear must be boolean or null")
    for name in ("material_negative", "agents_healthy", "push_healthy"):
        if not isinstance(values[name], bool):
            raise ValueError(f"{name} must be boolean")
    return RuntimeSafetyEnvelope(
        trade_date=date.fromisoformat(str(values["trade_date"])),
        symbol=str(values["symbol"]),
        generated_at_utc=datetime.fromisoformat(str(values["generated_at_utc"])),
        expires_at_utc=datetime.fromisoformat(str(values["expires_at_utc"])),
        negative_news_clear=negative_news_clear,
        material_negative=bool(values["material_negative"]),
        agents_healthy=bool(values["agents_healthy"]),
        push_healthy=bool(values["push_healthy"]),
        source_snapshot_ids=tuple(source_ids),
        provenance=str(values["provenance"]),
    )


def _execution_score(
    plan: AutonomousPaperPlan,
    facts: RealtimePlanFacts,
) -> float:
    midpoint = (facts.bid + facts.ask) / 2
    spread_ratio = (facts.ask - facts.bid) / midpoint
    maximum = float(plan.max_spread_ratio)
    return max(0.0, min(100.0, 100.0 * (1.0 - spread_ratio / maximum)))


def _position_state(
    plan: AutonomousPaperPlan,
    position: PaperPosition | None,
) -> tuple[int, float | None]:
    if position is None:
        return 0, None
    if position.symbol != plan.symbol:
        raise ValueError("broker position symbol does not match autonomous plan")
    if position.side.strip().lower() != "long":
        raise ValueError("autonomous Paper policy is permanently long-only")
    try:
        quantity = Decimal(position.qty)
        average_entry = Decimal(str(position.avg_entry_price))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("broker position state is invalid") from exc
    if quantity <= 0 or quantity != quantity.to_integral_value():
        raise ValueError("broker position quantity must be a positive whole number")
    if not average_entry.is_finite() or average_entry <= 0:
        raise ValueError("broker position average entry must be positive")
    return int(quantity), float(average_entry)


def _full_position_quantity(
    plan: AutonomousPaperPlan,
    *,
    equity: Decimal,
    entry_price: Decimal,
) -> int:
    if not equity.is_finite() or equity <= 0:
        raise ValueError("account equity must be finite and positive")
    risk_per_share = entry_price - plan.hard_stop
    if risk_per_share <= 0:
        return 0
    risk_budget = equity * plan.full_risk_fraction
    notional_budget = equity * plan.max_notional_fraction
    by_risk = int((risk_budget / risk_per_share).to_integral_value(rounding=ROUND_FLOOR))
    by_notional = int((notional_budget / entry_price).to_integral_value(rounding=ROUND_FLOOR))
    return min(by_risk, by_notional)


def _canonical_position_fraction(
    *,
    position_qty: int,
    full_quantity: int,
) -> float:
    if full_quantity <= 0:
        return 1.0
    for fraction in (0.10, 0.125, 0.15, 0.20, 0.25, 0.30, 0.50, 1.0):
        stage_quantity = int(
            (Decimal(full_quantity) * Decimal(str(fraction))).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        if position_qty == stage_quantity:
            return fraction
    return min(1.0, position_qty / full_quantity)


def _require_utc(value: datetime, *, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
