from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scripts.monitor_target import TARGETS, Snapshot, evaluate, summary

NOW = datetime(2026, 8, 4, 13, 30, tzinfo=UTC)


def _snapshot(last: float, *, vwap: float = 214.0, ratio: float = 1.5) -> Snapshot:
    return Snapshot(
        observed_at_utc=NOW,
        last=last,
        bid=last - 0.05,
        ask=last + 0.05,
        quote_at_utc=NOW,
        minute_volume=1000,
        session_volume=50000,
        vwap=vwap,
        volume_ratio=ratio,
        session_return=0.01,
        benchmark_return=None,
    )


def test_buy_signal_sets_buy_state() -> None:
    state: dict[str, object] = {"conditions": {}, "buy_seen": False, "below_vwap_polls": 0}
    events = evaluate(
        TARGETS["MRVL"], _snapshot(214.5), state, NOW, 3525, entry_monitoring_enabled=True
    )
    assert [event.kind for event in events] == ["buy_ready"]
    assert state["buy_seen"] is True
    assert state["buy_trigger_count"] == 1
    assert "第 1 次触发" in events[0].detail
    assert "5,250 股" in events[0].detail


def test_two_vwap_failures_emit_abandon() -> None:
    state: dict[str, object] = {"conditions": {}, "buy_seen": True, "below_vwap_polls": 0}
    first = evaluate(TARGETS["MRVL"], _snapshot(213.0), state, NOW, entry_monitoring_enabled=True)
    second = evaluate(
        TARGETS["MRVL"],
        _snapshot(213.0),
        state,
        NOW + timedelta(seconds=10),
        entry_monitoring_enabled=True,
    )
    assert not any(event.kind == "abandon" for event in first)
    assert any(event.kind == "abandon" for event in second)


def test_target_signal_and_summary_are_utf8_safe() -> None:
    state: dict[str, object] = {"conditions": {}, "buy_seen": True, "below_vwap_polls": 0}
    events = evaluate(
        TARGETS["MRVL"], _snapshot(222.6), state, NOW, 3525, entry_monitoring_enabled=True
    )
    body = summary(TARGETS["MRVL"], _snapshot(222.6), state, 3525)
    assert any(event.kind == "tp1" for event in events)
    assert "MRVL" in body and "成交量" in body and "买入监控：已关闭" in body and "?" not in body


def test_exit_only_mode_does_not_emit_entry_signal() -> None:
    state: dict[str, object] = {"conditions": {}, "buy_seen": False, "below_vwap_polls": 0}
    events = evaluate(TARGETS["MRVL"], _snapshot(214.5), state, NOW)
    assert not any(event.kind == "buy_ready" for event in events)


def test_watchlist_budget_tranches_are_one_thousand_dollars_each() -> None:
    assert sum(TARGETS["NVDA"].tranche_shares or ()) == 44
    assert sum(TARGETS["DIS"].tranche_shares or ()) == 96
    assert sum(TARGETS["LLY"].tranche_shares or ()) == 8
    assert all(TARGETS[symbol].budget_usd == 10_000 for symbol in ("NVDA", "DIS", "LLY"))


def test_exit_only_mode_does_not_emit_cutoff_abandon_signal() -> None:
    state: dict[str, object] = {"conditions": {}, "buy_seen": False, "below_vwap_polls": 0}
    after_cutoff = datetime(2026, 8, 4, 20, 0, tzinfo=UTC)
    events = evaluate(TARGETS["MRVL"], _snapshot(214.5), state, after_cutoff)
    assert not any(event.kind == "abandon" for event in events)
