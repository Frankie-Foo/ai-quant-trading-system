from datetime import UTC, datetime, timedelta

import polars as pl

from scripts.monitor_nvidia_limit import MonitorConfig, evaluate

NOW = datetime(2026, 8, 5, 14, 30, 0, tzinfo=UTC)
CONFIG = MonitorConfig(
    symbol="NVDA",
    limit_price=216.60,
    shares=40,
    poll_seconds=1,
    channel_id="channel",
)


def _quotes(ask: float) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["NVDA"],
            "ts_utc": [NOW - timedelta(seconds=2)],
            "bid_price": [ask - 0.02],
            "ask_price": [ask],
        }
    )


def test_limit_touch_emits_once_per_state_evaluation() -> None:
    signal = evaluate(CONFIG, _quotes(216.60), NOW)
    assert signal is not None
    assert signal.event == "limit_touch"
    assert "40" in signal.message


def test_quote_above_limit_does_not_trigger() -> None:
    assert evaluate(CONFIG, _quotes(216.61), NOW) is None
