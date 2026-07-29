"""One-tick runtime composition for the autonomous Alpaca Paper session."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from execution.alpaca_paper import PaperAccount, PaperPosition
from execution.autonomous_paper_session import (
    AutonomousPaperPlan,
    PaperSessionResult,
    PaperSessionSnapshot,
)
from kernel.adaptive_trade_plan import RealtimePlanFacts
from operations.autonomous_paper_config import AutonomousPaperPlanBundle
from operations.autonomous_policy_adapter import (
    AutonomousPolicySnapshotFactory,
    RuntimeSafetyEnvelope,
)

NEW_YORK = ZoneInfo("America/New_York")


class AutonomousMarketFactsPort(Protocol):
    def read(
        self,
        plan: AutonomousPaperPlan,
        *,
        observed_at_utc: datetime,
    ) -> RealtimePlanFacts: ...


class RuntimeBrokerReadPort(Protocol):
    def get_account(self) -> PaperAccount: ...

    def list_positions(self) -> tuple[PaperPosition, ...]: ...


class RuntimeOrchestratorPort(Protocol):
    def tick(
        self,
        plan: AutonomousPaperPlan,
        snapshot: PaperSessionSnapshot,
    ) -> PaperSessionResult: ...

    def fail_closed(
        self,
        plan: AutonomousPaperPlan,
        *,
        observed_at_utc: datetime,
        reason: str,
        exit_bid: Decimal | None = None,
        quote_asof_utc: datetime | None = None,
        quote_provenance: str | None = None,
    ) -> PaperSessionResult: ...


@dataclass(frozen=True)
class AutonomousRuntimeOutcome:
    plan_id: str
    symbol: str
    result: PaperSessionResult
    degraded_reasons: tuple[str, ...]


class AutonomousPaperRuntime:
    """Evaluate every registered plan once; sleeping and logging live in the CLI."""

    def __init__(
        self,
        *,
        plans: tuple[AutonomousPaperPlanBundle, ...],
        market: AutonomousMarketFactsPort,
        broker: RuntimeBrokerReadPort,
        orchestrator: RuntimeOrchestratorPort,
        envelope_loader: Callable[[Path], RuntimeSafetyEnvelope],
        snapshot_factory: AutonomousPolicySnapshotFactory | None = None,
    ):
        if not plans:
            raise ValueError("autonomous Paper runtime requires at least one plan")
        self.plans = plans
        self.market = market
        self.broker = broker
        self.orchestrator = orchestrator
        self.envelope_loader = envelope_loader
        self.snapshot_factory = snapshot_factory or AutonomousPolicySnapshotFactory()
        self._last_facts: dict[str, RealtimePlanFacts] = {}

    def tick_once(
        self,
        *,
        observed_at_utc: datetime,
    ) -> tuple[AutonomousRuntimeOutcome, ...]:
        _require_utc(observed_at_utc)
        positions = self.broker.list_positions()
        account = self.broker.get_account()
        equity = _account_equity(account)
        outcomes: list[AutonomousRuntimeOutcome] = []
        for bundle in self.plans:
            plan = bundle.plan
            if observed_at_utc.astimezone(NEW_YORK).date() != plan.trade_date:
                result = self.orchestrator.fail_closed(
                    plan,
                    observed_at_utc=observed_at_utc,
                    reason="trade_date_mismatch",
                )
                outcomes.append(
                    AutonomousRuntimeOutcome(
                        plan_id=plan.plan_id,
                        symbol=plan.symbol,
                        result=result,
                        degraded_reasons=("trade_date_mismatch",),
                    )
                )
                continue

            matching = tuple(
                position
                for position in positions
                if position.symbol == plan.symbol
            )
            if len(matching) > 1:
                result = self.orchestrator.fail_closed(
                    plan,
                    observed_at_utc=observed_at_utc,
                    reason="duplicate_broker_position",
                )
                outcomes.append(
                    AutonomousRuntimeOutcome(
                        plan_id=plan.plan_id,
                        symbol=plan.symbol,
                        result=result,
                        degraded_reasons=("duplicate_broker_position",),
                    )
                )
                continue
            position = matching[0] if matching else None
            envelope, safety_degradation = self._load_envelope(
                bundle,
                observed_at_utc=observed_at_utc,
            )
            try:
                facts = self.market.read(
                    plan,
                    observed_at_utc=observed_at_utc,
                )
                if facts.observed_at_utc != observed_at_utc:
                    raise ValueError("market facts observation time mismatch")
            except (KeyError, OSError, RuntimeError, ValueError):
                result = self._market_failure(
                    plan,
                    observed_at_utc=observed_at_utc,
                )
                outcomes.append(
                    AutonomousRuntimeOutcome(
                        plan_id=plan.plan_id,
                        symbol=plan.symbol,
                        result=result,
                        degraded_reasons=("market_facts_unavailable",),
                    )
                )
                continue
            self._last_facts[plan.symbol] = facts
            snapshot = self.snapshot_factory.build(
                plan=plan,
                evidence=bundle.evidence,
                facts=facts,
                envelope=envelope,
                position=position,
                account_equity=equity,
            )
            result = self.orchestrator.tick(plan, snapshot)
            outcomes.append(
                AutonomousRuntimeOutcome(
                    plan_id=plan.plan_id,
                    symbol=plan.symbol,
                    result=result,
                    degraded_reasons=safety_degradation,
                )
            )
        return tuple(outcomes)

    def fail_closed_plan(
        self,
        *,
        plan_id: str,
        observed_at_utc: datetime,
        reason: str,
    ) -> PaperSessionResult:
        """Apply an external runtime failure using only a fresh cached quote."""

        _require_utc(observed_at_utc)
        if not plan_id.strip() or not reason.strip():
            raise ValueError("fail-closed plan and reason are required")
        matches = tuple(
            bundle.plan for bundle in self.plans if bundle.plan.plan_id == plan_id
        )
        if len(matches) != 1:
            raise ValueError("fail-closed plan identity is unknown or ambiguous")
        plan = matches[0]
        cached = self._last_facts.get(plan.symbol)
        if cached is not None:
            age = (observed_at_utc - cached.quote_ts_utc).total_seconds()
            if (
                0 <= age <= 30
                and cached.quote_provenance is not None
                and cached.quote_provenance.strip()
            ):
                return self.orchestrator.fail_closed(
                    plan,
                    observed_at_utc=observed_at_utc,
                    reason=reason,
                    exit_bid=Decimal(str(cached.bid)),
                    quote_asof_utc=cached.quote_ts_utc,
                    quote_provenance=cached.quote_provenance,
                )
        return self.orchestrator.fail_closed(
            plan,
            observed_at_utc=observed_at_utc,
            reason=reason,
        )

    def _load_envelope(
        self,
        bundle: AutonomousPaperPlanBundle,
        *,
        observed_at_utc: datetime,
    ) -> tuple[RuntimeSafetyEnvelope | None, tuple[str, ...]]:
        try:
            envelope = self.envelope_loader(bundle.safety_envelope_path)
            if not envelope.is_current(observed_at_utc):
                return None, ("safety_envelope_stale",)
            if (
                envelope.trade_date != bundle.plan.trade_date
                or envelope.symbol != bundle.plan.symbol
            ):
                return None, ("safety_envelope_identity_mismatch",)
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            return None, ("safety_envelope_unavailable",)
        return envelope, ()

    def _market_failure(
        self,
        plan: AutonomousPaperPlan,
        *,
        observed_at_utc: datetime,
    ) -> PaperSessionResult:
        cached = self._last_facts.get(plan.symbol)
        if cached is not None:
            age = (observed_at_utc - cached.quote_ts_utc).total_seconds()
            if (
                0 <= age <= 30
                and cached.quote_provenance is not None
                and cached.quote_provenance.strip()
            ):
                return self.orchestrator.fail_closed(
                    plan,
                    observed_at_utc=observed_at_utc,
                    reason="market_facts_unavailable",
                    exit_bid=Decimal(str(cached.bid)),
                    quote_asof_utc=cached.quote_ts_utc,
                    quote_provenance=cached.quote_provenance,
                )
        return self.orchestrator.fail_closed(
            plan,
            observed_at_utc=observed_at_utc,
            reason="market_facts_unavailable",
        )


def _account_equity(account: PaperAccount) -> Decimal:
    try:
        equity = Decimal(account.equity)
    except InvalidOperation as exc:
        raise ValueError("Paper account equity is invalid") from exc
    if not equity.is_finite() or equity <= 0:
        raise ValueError("Paper account equity must be finite and positive")
    return equity


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("autonomous Paper runtime timestamp must be UTC")
