import json
import socket
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

import polars as pl
import pytest

from scripts import monitor_modern_momentum_forward as monitor

OPENED = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)


def test_forward_emits_current_signals_without_future_fill_or_fabricated_trades(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def no_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("network/order writes forbidden in this test")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(monitor, "ROOT", tmp_path)
    monkeypatch.setattr(monitor, "load_project_env", lambda *_: None)
    monkeypatch.setattr(monitor, "project_data_root", lambda *_: tmp_path / "data")
    pool_path = tmp_path / "data" / "accepted" / f"{monitor.SOURCE}-fixture" / "data.parquet"
    pool_path.parent.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["TEST"],
            "session_date": [OPENED.date()],
            "price": [96.0],
            "forward_market_cap": [2e9],
            "rvol": [2.0],
        }
    ).write_parquet(pool_path)
    minutes = iter([28, 28, 29, 29, 380])

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Self:
            current = OPENED + timedelta(minutes=next(minutes))
            return cls.fromtimestamp(current.timestamp(), tz=UTC)

    monkeypatch.setattr(monitor, "datetime", Clock)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    messages: list[str] = []

    class Push:
        def push(self, body: str) -> str:
            messages.append(body)
            return f"message-{len(messages)}"

        def close(self) -> None:
            pass

    monkeypatch.setattr(monitor, "_push_client", Push)

    def fetch_bars(_symbols: tuple[str, ...], _start: datetime, end: datetime) -> pl.DataFrame:
        rows = []
        for minute in range(int((end - OPENED).total_seconds() / 60)):
            close = 99.4 + min(minute, 14) * 0.035
            if 15 <= minute < 26:
                close = 99.85
            if minute >= 26:
                close = 100.45 + (minute - 26) * 0.08
            rows.append(
                {
                    "symbol": "TEST",
                    "ts_utc": OPENED + timedelta(minutes=minute),
                    "open": close - 0.02,
                    "high": close + 0.08,
                    "low": close - 0.08,
                    "close": close,
                    "volume": 10_000,
                }
            )
        return pl.DataFrame(rows)

    def fetch_quotes(_symbols: tuple[str, ...], _start: datetime, end: datetime) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "symbol": ["TEST"],
                "ts_utc": [end - timedelta(seconds=1)],
                "bid_price": [100.5],
                "ask_price": [100.6],
                "source": ["test"],
                "feed": ["sip"],
            }
        )

    monkeypatch.setattr(monitor, "fetch_bars", fetch_bars)
    monkeypatch.setattr(monitor, "fetch_quotes", fetch_quotes, raising=False)
    monkeypatch.setattr(sys, "argv", ["forward", "--trade-date", "2026-08-17"])
    monitor.main()

    state = json.loads((tmp_path / "runs/modern-momentum/2026-08-17/state.json").read_text())
    assert state["orders_enabled"] is False
    assert state["strategy_manifest"]["effective_config"]["minimum_premarket_rvol"] == 1.5
    assert [event["type"] for event in state["events"]] == ["shadow_signal", "shadow_signal"]
    assert [event["signal_ts_utc"] for event in state["events"]] == [
        "2026-08-17 13:58:00+00:00",
        "2026-08-17 13:59:00+00:00",
    ]
    assert all("pnl" not in event and "entry_px" not in event for event in state["events"])
    assert len(messages) == 2
