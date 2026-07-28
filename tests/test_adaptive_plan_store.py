from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from kernel.adaptive_trade_plan import (
    BaselineTradePlan,
    PlanAction,
    PlanMode,
    RealtimePlanFacts,
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


def _facts(minute: int) -> RealtimePlanFacts:
    observed = OPEN + timedelta(minutes=minute, seconds=5)
    return RealtimePlanFacts(
        observed_at_utc=observed,
        quote_ts_utc=observed - timedelta(seconds=1),
        bid=100.99,
        ask=101.01,
        last_price=101.0,
        session_vwap=100.5,
        completed_one_minute_bar_utc=OPEN + timedelta(minutes=minute),
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


def test_plan_risk_envelope_is_immutable_for_existing_id(tmp_path: Path) -> None:
    store = AdaptivePlanStore(tmp_path / "adaptive.sqlite3")
    store.register(_plan())

    with pytest.raises(ValueError, match="immutable"):
        store.register(replace(_plan(), max_risk_dollars=600.0))


def test_evaluation_is_persistent_and_same_bar_does_not_emit_duplicate_event(
    tmp_path: Path,
) -> None:
    store = AdaptivePlanStore(tmp_path / "adaptive.sqlite3")
    store.register(_plan())

    armed = store.evaluate(_plan().plan_id, _facts(6), position=None)
    repeated = store.evaluate(
        _plan().plan_id,
        replace(
            _facts(6),
            observed_at_utc=_facts(6).observed_at_utc + timedelta(seconds=15),
            quote_ts_utc=_facts(6).quote_ts_utc + timedelta(seconds=15),
        ),
        position=None,
    )
    ready = store.evaluate(_plan().plan_id, _facts(7), position=None)

    assert armed.decision.action is PlanAction.ARM_ENTRY
    assert armed.sequence == 1
    assert repeated.decision.action is PlanAction.NO_ACTION
    assert repeated.sequence is None
    assert ready.decision.action is PlanAction.ENTER_PROBE
    assert ready.sequence == 2
    assert store.runtime(_plan().plan_id).revision == 1
    assert [event["sequence"] for event in store.events_after(0)] == [1, 2]


def test_dashboard_contains_real_runtime_and_never_order_authority(
    tmp_path: Path,
) -> None:
    store = AdaptivePlanStore(tmp_path / "adaptive.sqlite3")
    store.register(_plan())
    store.evaluate(_plan().plan_id, _facts(6), position=None)
    store.evaluate(_plan().plan_id, _facts(7), position=None)

    dashboard = store.dashboard()

    assert dashboard["schema_version"] == "adaptive_trade_dashboard.v1"
    assert dashboard["orders_authorized"] is False
    assert dashboard["plans"][0]["symbol"] == "XYZ"
    assert dashboard["plans"][0]["runtime"]["state"] == "entry_ready"
    assert dashboard["plans"][0]["latest_decision"]["action"] == "enter_probe"
    assert dashboard["latest_sequence"] == 2


def test_dashboard_only_shows_latest_registered_trade_date(tmp_path: Path) -> None:
    store = AdaptivePlanStore(tmp_path / "adaptive.sqlite3")
    old = replace(
        _plan(),
        plan_id="plan-20260727-OLD",
        symbol="OLD",
        trade_date=date(2026, 7, 27),
        entry_window_end_utc=_plan().entry_window_end_utc - timedelta(days=1),
        force_exit_utc=_plan().force_exit_utc - timedelta(days=1),
    )
    store.register(old)
    store.register(_plan())

    dashboard = store.dashboard()

    assert [item["symbol"] for item in dashboard["plans"]] == ["XYZ"]


def test_runtime_survives_store_reopen(tmp_path: Path) -> None:
    path = tmp_path / "adaptive.sqlite3"
    first = AdaptivePlanStore(path)
    first.register(_plan())
    first.evaluate(_plan().plan_id, _facts(6), position=None)

    reopened = AdaptivePlanStore(path)

    assert reopened.runtime(_plan().plan_id).consecutive_confirmations == 1
    assert reopened.dashboard()["plans"][0]["runtime"]["state"] == "armed"


def test_cleared_blocker_can_be_emitted_again_without_poll_noise(
    tmp_path: Path,
) -> None:
    store = AdaptivePlanStore(tmp_path / "adaptive.sqlite3")
    store.register(_plan())
    store.evaluate(_plan().plan_id, _facts(6), position=None)
    blocked = replace(
        _facts(6),
        observed_at_utc=_facts(6).observed_at_utc + timedelta(seconds=10),
        quote_ts_utc=_facts(6).quote_ts_utc + timedelta(seconds=10),
        market_risk_off=True,
    )
    first_block = store.evaluate(_plan().plan_id, blocked, position=None)
    cleared = store.evaluate(
        _plan().plan_id,
        replace(
            _facts(6),
            observed_at_utc=_facts(6).observed_at_utc + timedelta(seconds=20),
            quote_ts_utc=_facts(6).quote_ts_utc + timedelta(seconds=20),
        ),
        position=None,
    )
    second_block = store.evaluate(
        _plan().plan_id,
        replace(
            blocked,
            observed_at_utc=blocked.observed_at_utc + timedelta(seconds=20),
            quote_ts_utc=blocked.quote_ts_utc + timedelta(seconds=20),
        ),
        position=None,
    )

    assert first_block.sequence == 2
    assert cleared.sequence is None
    assert second_block.sequence == 3
