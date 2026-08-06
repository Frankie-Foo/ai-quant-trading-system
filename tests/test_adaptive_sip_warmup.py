from __future__ import annotations

from datetime import UTC, datetime

import polars as pl
import pytest

from execution.alpaca_sip_stream import SipBar, SipQuote, SipTrade
from operations.adaptive_sip_warmup import build_warmup_events


def _frames() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    bars = pl.DataFrame(
        {
            "symbol": ["XYZ"],
            "ts_utc": [datetime(2026, 7, 28, 13, 30, tzinfo=UTC)],
            "open": [100.0],
            "high": [101.0],
            "low": [99.5],
            "close": [100.8],
            "volume": [10_000],
            "trade_count": [80],
            "vwap": [100.4],
            "source": ["cloud.alpaca.market_data"],
            "feed": ["sip"],
            "adjustment": ["split_adjusted"],
        }
    )
    quotes = pl.DataFrame(
        {
            "symbol": ["XYZ"],
            "ts_utc": [datetime(2026, 7, 28, 13, 31, tzinfo=UTC)],
            "bid_price": [100.7],
            "bid_size": [10],
            "ask_price": [100.8],
            "ask_size": [12],
            "source": ["cloud.alpaca.market_data"],
            "feed": ["sip"],
        }
    )
    trades = pl.DataFrame(
        {
            "symbol": ["XYZ"],
            "ts_utc": [datetime(2026, 7, 28, 13, 31, tzinfo=UTC)],
            "trade_id": [7],
            "exchange": ["Q"],
            "price": [100.75],
            "size": [100],
            "conditions": [["@"]],
            "tape": ["C"],
            "source": ["cloud.alpaca.market_data"],
            "feed": ["sip"],
        }
    )
    return bars, quotes, trades


def test_build_warmup_events_preserves_observed_values_and_provenance() -> None:
    bars, quotes, trades = _frames()

    events = build_warmup_events(bars=bars, quotes=quotes, trades=trades)

    assert isinstance(events[0], SipBar)
    assert isinstance(events[1], SipQuote)
    assert isinstance(events[2], SipTrade)
    assert events[0].provenance == (
        "cloud.alpaca.market_data:sip:historical_bar:split_adjusted"
    )
    assert events[2].conditions == ("@",)


def test_build_warmup_events_rejects_delayed_or_invented_bar_values() -> None:
    bars, quotes, trades = _frames()
    delayed = bars.with_columns(pl.lit("delayed_sip").alias("feed"))
    missing_vwap = bars.with_columns(pl.lit(None).cast(pl.Float64).alias("vwap"))

    with pytest.raises(ValueError, match="licensed SIP"):
        build_warmup_events(bars=delayed, quotes=quotes, trades=trades)
    with pytest.raises(ValueError, match="vwap"):
        build_warmup_events(bars=missing_vwap, quotes=quotes, trades=trades)
