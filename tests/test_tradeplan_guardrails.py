from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from kernel.config import load_config
from kernel.guardrails import GuardrailContext, RiskCode, arbitrate_trade
from kernel.tradeplan import TradePlan

NOW = datetime(2026, 7, 21, 14, 37, tzinfo=UTC)


def _plan() -> TradePlan:
    return TradePlan(
        plan_id="orb5-20260721-AAPL-001",
        trace_id="trace-20260721-AAPL-001",
        strategy_version="orb5.v1",
        symbol="AAPL",
        trade_date=date(2026, 7, 21),
        decision_asof_utc=NOW - timedelta(seconds=15),
        created_at_utc=NOW,
        quantity=10,
        reference_price=Decimal("225.00"),
        take_profit_price=Decimal("229.00"),
        stop_loss_price=Decimal("223.00"),
        time_stop_utc=datetime(2026, 7, 21, 19, 55, tzinfo=UTC),
        source_snapshot_ids=("selection-20260721", "sip-bar-20260721T143600Z"),
        provenance="kernel.tradeplan.test",
    )


def _context(**updates: object) -> GuardrailContext:
    payload: dict[str, object] = {
        "evaluated_at_utc": NOW,
        "market_data_asof_utc": NOW - timedelta(seconds=20),
        "market_data_feed": "sip",
        "paper_endpoint": True,
        "kill_switch_active": False,
        "market_open": True,
        "account_active": True,
        "account_blocked": False,
        "trading_blocked": False,
        "equity": Decimal("100000"),
        "daily_pnl": Decimal("-100"),
        "gross_exposure": Decimal("10000"),
        "open_position_symbols": ("MSFT",),
        "buying_power": Decimal("50000"),
        "sizing_notional_cap": Decimal("2250"),
        "selected_symbols": ("AAPL", "MSFT"),
        "selection_snapshot_ids": ("selection-20260721",),
    }
    payload.update(updates)
    return GuardrailContext.model_validate(payload)


def test_trade_plan_is_permanently_long_only_and_bracket_protected() -> None:
    plan = _plan()

    assert plan.side == "buy"
    assert plan.extended_hours is False
    assert plan.stop_loss_price < plan.reference_price < plan.take_profit_price
    with pytest.raises(ValidationError):
        TradePlan.model_validate({**plan.model_dump(), "side": "sell"})


def test_guardrails_approve_complete_fresh_paper_context() -> None:
    verdict = arbitrate_trade(_plan(), _context(), load_config("config.yaml"))

    assert verdict.approved is True
    assert verdict.failure_code is None
    assert [check.priority for check in verdict.checks] == [
        "P0",
        "P0",
        "P0",
        "P0",
        "P0",
        "P0",
        "P1",
        "P1",
        "P1",
        "P1",
        "P2",
        "P2",
        "P2",
    ]


def test_p0_kill_switch_blocks_before_all_other_failures() -> None:
    context = _context(
        kill_switch_active=True,
        paper_endpoint=False,
        market_data_feed="iex",
        market_data_asof_utc=NOW - timedelta(minutes=10),
        daily_pnl=Decimal("-99999"),
    )

    verdict = arbitrate_trade(_plan(), context, load_config("config.yaml"))

    assert verdict.approved is False
    assert verdict.failure_code is RiskCode.P0_KILL_SWITCH
    assert len(verdict.checks) == 1


def test_daily_loss_and_sizing_caps_fail_closed() -> None:
    cfg = load_config("config.yaml")
    daily_loss = arbitrate_trade(
        _plan(),
        _context(daily_pnl=Decimal("-1500")),
        cfg,
    )
    oversized = arbitrate_trade(
        _plan(),
        _context(sizing_notional_cap=Decimal("2000")),
        cfg,
    )

    assert daily_loss.failure_code is RiskCode.P1_DAILY_LOSS
    assert oversized.failure_code is RiskCode.P2_SIZING_CAP


def test_guardrail_context_rejects_naive_or_future_market_timestamp() -> None:
    with pytest.raises(ValidationError):
        _context(market_data_asof_utc=datetime(2026, 7, 21, 14, 36))
    with pytest.raises(ValidationError):
        _context(market_data_asof_utc=NOW + timedelta(seconds=1))
