from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from kernel.adaptive_trade_plan import (
    BaselineTradePlan,
    PlanAction,
    PlanMode,
    RealtimePlanFacts,
)
from operations.adaptive_plan_coordinator import (
    AdaptivePlanCoordinator,
    BrokerPositionObservation,
)
from operations.adaptive_plan_store import AdaptivePlanStore

OPEN = datetime(2026, 7, 28, 13, 30, tzinfo=UTC)


def _plan() -> BaselineTradePlan:
    return BaselineTradePlan(
        plan_id="plan-20260728-XYZ",
        symbol="XYZ",
        trade_date=date(2026, 7, 28),
        mode=PlanMode.CATALYST,
        entry_window_end_utc=OPEN + timedelta(hours=2),
        force_exit_utc=OPEN + timedelta(hours=6, minutes=25),
        hard_stop=99.0,
        max_risk_dollars=300.0,
        max_notional=20_000.0,
        probe_fraction=0.25,
        max_spread_ratio=0.0025,
        soft_cooldown=timedelta(minutes=3),
        max_soft_revisions=3,
    )


def _facts(observed: datetime, *, price: float = 101.0) -> RealtimePlanFacts:
    return RealtimePlanFacts(
        observed_at_utc=observed,
        quote_ts_utc=observed - timedelta(seconds=1),
        bid=price - 0.01,
        ask=price + 0.01,
        last_price=price,
        session_vwap=100.5,
        completed_one_minute_bar_utc=observed.replace(second=0, microsecond=0),
        one_minute_trigger=True,
        five_minute_confirmed=True,
        fifteen_minute_confirmed=True,
        green_volume_ratio=1.8,
        relative_strength=0.01,
        benchmark_above_vwap=True,
        sector_above_vwap=True,
        market_risk_off=False,
        order_flow_imbalance=0.25,
        catalyst_score=0.82,
        data_complete=True,
    )


class FakeMarket:
    def __init__(self, facts: RealtimePlanFacts):
        self.facts = facts

    def read(
        self,
        plan: BaselineTradePlan,
        *,
        observed_at_utc: datetime,
    ) -> RealtimePlanFacts:
        assert plan.plan_id == _plan().plan_id
        return replace(
            self.facts,
            observed_at_utc=observed_at_utc,
            quote_ts_utc=observed_at_utc - timedelta(seconds=1),
        )


class FakeBroker:
    def __init__(self, position: BrokerPositionObservation | None):
        self.observation = position

    def position(
        self,
        symbol: str,
        *,
        observed_at_utc: datetime,
    ) -> BrokerPositionObservation | None:
        assert symbol == "XYZ"
        if self.observation is None:
            return None
        return replace(self.observation, observed_at_utc=observed_at_utc)


def test_broker_position_is_authority_and_hard_stop_is_applied(tmp_path: Path) -> None:
    store = AdaptivePlanStore(tmp_path / "adaptive.sqlite3")
    store.register(_plan())
    now = OPEN + timedelta(minutes=20)
    coordinator = AdaptivePlanCoordinator(
        store=store,
        market=FakeMarket(_facts(now, price=98.95)),
        broker=FakeBroker(
            BrokerPositionObservation(
                symbol="XYZ",
                shares=50,
                average_entry=101.0,
                observed_at_utc=now,
            )
        ),
    )

    result = coordinator.tick(_plan().plan_id, observed_at_utc=now)

    assert result.decision.action is PlanAction.EXIT_NOW
    assert result.decision.order_authorized is False
    assert result.position_source == "broker"


def test_flat_broker_does_not_reuse_stale_local_position(tmp_path: Path) -> None:
    store = AdaptivePlanStore(tmp_path / "adaptive.sqlite3")
    store.register(_plan())
    now = OPEN + timedelta(minutes=6)
    coordinator = AdaptivePlanCoordinator(
        store=store,
        market=FakeMarket(_facts(now)),
        broker=FakeBroker(None),
    )

    result = coordinator.tick(_plan().plan_id, observed_at_utc=now)

    assert result.decision.action is PlanAction.ARM_ENTRY
    assert result.position_source == "broker_flat"
