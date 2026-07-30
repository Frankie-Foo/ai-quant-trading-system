"""Runtime coordinator for market facts, broker reconciliation and plan storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from kernel.adaptive_trade_plan import (
    BaselineTradePlan,
    PlanDecision,
    PositionFacts,
    RealtimePlanFacts,
)
from operations.adaptive_plan_store import AdaptivePlanStore


class MarketFactsPort(Protocol):
    def read(
        self,
        plan: BaselineTradePlan,
        *,
        observed_at_utc: datetime,
    ) -> RealtimePlanFacts: ...


@dataclass(frozen=True)
class BrokerPositionObservation:
    symbol: str
    shares: int
    average_entry: float
    observed_at_utc: datetime


class BrokerPositionPort(Protocol):
    def position(
        self,
        symbol: str,
        *,
        observed_at_utc: datetime,
    ) -> BrokerPositionObservation | None: ...


@dataclass(frozen=True)
class CoordinatedEvaluation:
    decision: PlanDecision
    sequence: int | None
    position_source: str


class AdaptivePlanCoordinator:
    """One small interface over external facts and atomic deterministic evaluation."""

    _MAX_BROKER_OBSERVATION_AGE = timedelta(seconds=30)

    def __init__(
        self,
        *,
        store: AdaptivePlanStore,
        market: MarketFactsPort,
        broker: BrokerPositionPort,
    ):
        self.store = store
        self.market = market
        self.broker = broker

    def tick(
        self,
        plan_id: str,
        *,
        observed_at_utc: datetime,
    ) -> CoordinatedEvaluation:
        if (
            observed_at_utc.tzinfo is None
            or observed_at_utc.utcoffset() != timedelta(0)
        ):
            raise ValueError("observed_at_utc must be timezone-aware UTC")
        plan = self.store.plan(plan_id)
        runtime = self.store.runtime(plan_id)
        facts = self.market.read(plan, observed_at_utc=observed_at_utc)
        if facts.observed_at_utc != observed_at_utc:
            raise ValueError("market facts must use the coordinator observation time")
        broker_position = self.broker.position(
            plan.symbol,
            observed_at_utc=observed_at_utc,
        )
        position: PositionFacts | None = None
        position_source = "broker_flat"
        if broker_position is not None:
            if broker_position.symbol != plan.symbol:
                raise ValueError("broker returned a different symbol")
            age = observed_at_utc - broker_position.observed_at_utc
            if age < timedelta(0) or age > self._MAX_BROKER_OBSERVATION_AGE:
                raise ValueError("broker position observation is stale or from the future")
            position = PositionFacts(
                symbol=broker_position.symbol,
                shares=broker_position.shares,
                average_entry=broker_position.average_entry,
                broker_asof_utc=broker_position.observed_at_utc,
                current_stop=max(plan.hard_stop, runtime.protective_stop),
            )
            position_source = "broker"
        stored = self.store.evaluate(plan_id, facts, position=position)
        return CoordinatedEvaluation(
            decision=stored.decision,
            sequence=stored.sequence,
            position_source=position_source,
        )
