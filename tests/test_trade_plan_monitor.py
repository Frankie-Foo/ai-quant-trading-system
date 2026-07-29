from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import polars as pl
import pytest

from scripts.monitor_trade_plan import (
    Position,
    Quote,
    Signal,
    SymbolPlan,
    _position_size,
    _send_vps,
    build_position_plan_message,
    evaluate_position_stop,
    evaluate_position_time_exit,
    evaluate_symbol,
    load_config,
)

OPEN = datetime(2026, 7, 27, 13, 30, tzinfo=UTC)


def _plan() -> SymbolPlan:
    return SymbolPlan(
        symbol="BX",
        priority=1,
        premarket_high=132.28,
        premarket_vwap=131.36,
        support_low=131.20,
        support_high=131.50,
        reclaim_price=131.60,
        pullback_stop=130.90,
        breakout_stop=131.20,
        max_spread_ratio=0.0025,
        max_chase_ratio=0.01,
        max_risk=350,
        max_notional=35_000,
        minimum_opening_dollar_volume=0,
    )


def _bars(*, opening_close: float = 131.70) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for index in range(5):
        close = opening_close if index == 4 else 131.60 + index * 0.02
        rows.append(
            {
                "symbol": "BX",
                "ts_utc": OPEN + timedelta(minutes=index),
                "open": close - 0.05,
                "high": max(close + 0.08, 131.75),
                "low": min(close - 0.08, 131.52),
                "close": close,
                "volume": 100,
                "vwap": close - 0.02,
            }
        )
    rows.append(
        {
            "symbol": "BX",
            "ts_utc": OPEN + timedelta(minutes=5),
            "open": 131.40,
            "high": 131.75,
            "low": 131.30,
            "close": 131.70,
            "volume": 200,
            "vwap": 131.55,
        }
    )
    return pl.DataFrame(rows).with_columns(
        pl.col("ts_utc").cast(pl.Datetime("ms", "UTC"))
    )


def test_position_size_respects_risk_and_notional_caps() -> None:
    assert _position_size(_plan(), entry=132.40, stop=131.20) == 264


def test_fresh_bid_at_stop_emits_stop_loss() -> None:
    now = OPEN + timedelta(minutes=10)
    signal = evaluate_position_stop(
        Position(symbol="BX", entry=131.90, shares=260, stop=130.90),
        Quote(
            symbol="BX",
            observed_at_utc=now - timedelta(seconds=2),
            bid=130.90,
            ask=130.95,
        ),
        now_utc=now,
    )
    assert signal is not None
    assert signal.event == "stop_loss"
    assert "-0.76%" in signal.message
    assert "$" not in signal.message


def test_stale_quote_never_emits_stop_loss() -> None:
    now = OPEN + timedelta(minutes=10)
    signal = evaluate_position_stop(
        Position(symbol="BX", entry=131.90, shares=260, stop=130.90),
        Quote(
            symbol="BX",
            observed_at_utc=now - timedelta(seconds=31),
            bid=130.80,
            ask=130.85,
        ),
        now_utc=now,
    )
    assert signal is None


def test_pullback_reclaim_emits_buy_ready() -> None:
    now = OPEN + timedelta(minutes=6, seconds=5)
    signal = evaluate_symbol(
        _plan(),
        _bars(),
        Quote(
            symbol="BX",
            observed_at_utc=now - timedelta(seconds=2),
            bid=131.68,
            ask=131.72,
        ),
        market_open_utc=OPEN,
        now_utc=now,
    )
    assert signal is not None
    assert signal.event == "buy_ready"
    assert signal.reason == "pullback_reclaim"


def test_opening_range_close_below_support_emits_abandon() -> None:
    now = OPEN + timedelta(minutes=6, seconds=5)
    signal = evaluate_symbol(
        _plan(),
        _bars(opening_close=131.10),
        Quote(
            symbol="BX",
            observed_at_utc=now - timedelta(seconds=2),
            bid=131.08,
            ask=131.12,
        ),
        market_open_utc=OPEN,
        now_utc=now,
    )
    assert signal is not None
    assert signal.event == "abandon"
    assert signal.reason == "opening_range_closed_below_support"


def test_vps_push_requires_bot_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VPS_LIVERMORE_APP_SECRET", "test-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-vertu-bot-app-id"].startswith("vbot_")
        assert request.headers["x-vertu-bot-app-secret"] == "test-secret"
        assert request.headers["content-type"] == "application/json; charset=utf-8"
        assert "止损 -0.58%" in request.content.decode("utf-8")
        return httpx.Response(
            200,
            json={
                "message": {
                    "id": "message-1",
                    "sender_type": "bot",
                }
            },
        )

    signal = Signal("stop_loss", "KKR", "test", "止损 -0.58%", "test:1")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert _send_vps("channel-1", signal, client=client) == "message-1"


def test_vps_push_rejects_user_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VPS_LIVERMORE_APP_SECRET", "test-secret")
    signal = Signal("stop_loss", "KKR", "test", "止损 -0.58%", "test:2")
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={"message": {"id": "message-2", "sender_type": "user"}},
        )
    )
    with (
        httpx.Client(transport=transport) as client,
        pytest.raises(RuntimeError, match="sender identity was not a bot"),
    ):
        _send_vps("channel-1", signal, client=client)


def test_position_plan_uses_utf8_and_percentage_returns() -> None:
    message = build_position_plan_message(
        Position(symbol="KKR", entry=101.89, shares=50, stop=101.30),
        account_value=100_000,
        targets=((103.15, 25), (104.05, 25)),
    )
    assert "【利弗莫尔｜KKR持仓执行预案】" in message
    assert "相对成本 -0.58%" in message
    assert "相对成本 +1.24%" in message
    assert "相对成本 +2.12%" in message
    assert "加权收益 +1.68%" in message
    assert "北京时间00:00做强弱决策" in message
    assert "北京时间01:00无条件清空全部剩余仓位" in message
    assert "?" not in message
    assert "$" not in message


def test_position_time_exit_escalates_before_close() -> None:
    config = load_config(
        Path(__file__).resolve().parents[1]
        / "config"
        / "trade_plan_2026-07-27.json"
    )
    position = Position(symbol="BX", entry=131.00, shares=50, stop=130.50)
    decision_time = datetime(2026, 7, 27, 16, 1, tzinfo=UTC)
    hold = evaluate_position_time_exit(
        position,
        config,
        _bars(),
        Quote(
            symbol="BX",
            observed_at_utc=decision_time - timedelta(seconds=2),
            bid=131.68,
            ask=131.72,
        ),
        now_utc=decision_time,
    )
    force = evaluate_position_time_exit(
        position,
        config,
        pl.DataFrame(),
        None,
        now_utc=datetime(2026, 7, 27, 17, 1, tzinfo=UTC),
    )
    violation = evaluate_position_time_exit(
        position,
        config,
        pl.DataFrame(),
        None,
        now_utc=datetime(2026, 7, 27, 20, 1, tzinfo=UTC),
    )
    assert hold is not None and hold.event == "hold_to_force_exit"
    assert force is not None and force.event == "force_exit"
    assert violation is not None and violation.event == "overnight_violation"


def test_position_exits_at_midnight_when_market_data_is_missing() -> None:
    config = load_config(
        Path(__file__).resolve().parents[1]
        / "config"
        / "trade_plan_2026-07-27.json"
    )
    signal = evaluate_position_time_exit(
        Position(symbol="KKR", entry=101.89, shares=50, stop=101.30),
        config,
        pl.DataFrame(),
        None,
        now_utc=datetime(2026, 7, 27, 16, 1, tzinfo=UTC),
    )
    assert signal is not None and signal.event == "exit_now"
