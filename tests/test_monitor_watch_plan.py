from datetime import UTC, datetime, timedelta

import polars as pl

from scripts.monitor_watch_plan import WatchPlan, evaluate

NOW = datetime(2026, 8, 5, 14, 50, 0, tzinfo=UTC)
PLAN = WatchPlan(
    symbol="ZETA",
    entry_mode="above",
    entry_trigger=28.40,
    first_shares=88,
    full_shares=176,
    add_trigger=28.33,
    add_mode="retest",
    hard_stop=27.83,
    tp1=29.25,
    tp2=30.10,
)


def _quote(bid: float, ask: float) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["ZETA"],
            "ts_utc": [NOW - timedelta(seconds=2)],
            "bid_price": [bid],
            "ask_price": [ask],
        }
    )


def test_stop_limit_entry_triggers_on_upward_cross() -> None:
    state = {"phase": "watching", "notified": {}}
    signal = evaluate(PLAN, state, _quote(28.40, 28.41), NOW)
    assert signal is not None
    assert signal.event == "entry"
    assert "88" in signal.message


def test_hard_stop_only_applies_after_entry_alert() -> None:
    state = {"phase": "active", "active": True, "notified": {}}
    signal = evaluate(PLAN, state, _quote(27.82, 27.84), NOW)
    assert signal is not None
    assert signal.event == "stop"


def test_no_entry_when_stop_limit_is_not_reached() -> None:
    state = {"phase": "watching", "notified": {}}
    assert evaluate(PLAN, state, _quote(28.20, 28.21), NOW) is None
