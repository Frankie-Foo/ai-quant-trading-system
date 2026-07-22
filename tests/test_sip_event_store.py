from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from execution.alpaca_sip_stream import SipBar, SipQuote
from execution.sip_store import SipEventStore


def test_store_deduplicates_bars_and_keeps_last_quote_per_second(tmp_path: Path) -> None:
    store = SipEventStore(tmp_path / "sip.sqlite3")
    bar = SipBar(
        symbol="AAPL",
        ts_utc=datetime(2026, 7, 21, 14, 36, tzinfo=UTC),
        open=224.0,
        high=225.1,
        low=223.9,
        close=225.0,
        volume=1000,
        trade_count=50,
        vwap=224.7,
        provenance="alpaca.sip.websocket@test",
    )
    first_quote = SipQuote(
        symbol="AAPL",
        ts_utc=datetime(2026, 7, 21, 14, 37, 1, 100000, tzinfo=UTC),
        bid_price=224.9,
        bid_size=2,
        ask_price=225.0,
        ask_size=4,
        provenance="alpaca.sip.websocket@test",
    )
    last_quote = first_quote.model_copy(
        update={
            "ts_utc": datetime(2026, 7, 21, 14, 37, 1, 900000, tzinfo=UTC),
            "bid_price": 224.95,
        }
    )

    store.append(bar)
    store.append(bar)
    store.append(first_quote)
    store.append(last_quote)

    counts = store.counts()
    latest = store.latest_quote("AAPL")
    assert counts == {"bars": 1, "quote_seconds": 1}
    assert latest is not None
    assert latest.bid_price == 224.95
    assert latest.ts_utc.microsecond == 900000
    bars = store.bars_for_symbol(
        "aapl",
        start_utc=datetime(2026, 7, 21, 14, 30, tzinfo=UTC),
        end_utc=datetime(2026, 7, 21, 14, 40, tzinfo=UTC),
    )
    assert bars.height == 1
    assert bars.get_column("ts_utc")[0] == bar.ts_utc
