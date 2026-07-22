from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from execution.alpaca_sip_stream import SipQuote
from execution.live_planning import PlannedTrade, build_live_trade_plan
from execution.locked_selection import LockedCandidate
from kernel.config import load_config
from kernel.signals import OrbIntent

NOW = datetime(2026, 7, 21, 13, 36, 1, tzinfo=UTC)


def _candidate() -> LockedCandidate:
    return LockedCandidate(
        symbol="AAPL",
        selection_rank=1,
        rvol=4.0,
        price=225.0,
        adv_usd=1_000_000_000.0,
        atr_pct=0.03,
        tier="mega",
    )


def _intent() -> OrbIntent:
    return OrbIntent(
        symbol="AAPL",
        triggered=True,
        reason="triggered",
        opening_range_high=225.0,
        opening_range_low=220.0,
        trigger_ts_utc=datetime(2026, 7, 21, 13, 35, tzinfo=UTC),
        planned_entry_ts_utc=datetime(2026, 7, 21, 13, 36, tzinfo=UTC),
        provenance="kernel.signals.orb5_intent@test",
    )


def _quote(**updates: object) -> SipQuote:
    payload: dict[str, object] = {
        "symbol": "AAPL",
        "ts_utc": NOW - timedelta(milliseconds=100),
        "bid_price": 224.99,
        "bid_size": 5,
        "ask_price": 225.01,
        "ask_size": 6,
        "provenance": "alpaca.sip.websocket@test",
    }
    payload.update(updates)
    return SipQuote.model_validate(payload)


def _build(quote: SipQuote) -> PlannedTrade:
    return build_live_trade_plan(
        _candidate(),
        _intent(),
        quote,
        trade_date=date(2026, 7, 21),
        selection_snapshot_id="selection-1",
        account_equity=100_000.0,
        is_half_day=False,
        created_at_utc=NOW,
        cfg=load_config("config.yaml"),
    )


def test_live_plan_uses_known_ask_and_actual_account_equity() -> None:
    planned = _build(_quote())

    assert planned.plan.reference_price == Decimal("225.01")
    assert planned.sizing.capital_base == 100_000.0
    assert planned.plan.quantity == planned.sizing.shares
    assert planned.plan.stop_loss_price < planned.plan.reference_price
    assert planned.plan.take_profit_price > planned.plan.reference_price
    assert planned.plan.time_stop_utc == datetime(2026, 7, 21, 19, 55, tzinfo=UTC)


def test_live_plan_rejects_crossed_or_stale_quote() -> None:
    with pytest.raises(ValueError, match="NBBO"):
        _build(_quote(bid_price=225.1, ask_price=225.0))
    with pytest.raises(ValueError, match="stale"):
        _build(_quote(ts_utc=NOW - timedelta(seconds=91)))
