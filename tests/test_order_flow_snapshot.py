from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from data_plane.storage import persist_snapshot
from scripts.build_order_flow_snapshot import build_order_flow_snapshot

ASOF = datetime(2026, 7, 28, 14, 1, tzinfo=UTC)


def _trades() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["FAST", "FAST"],
            "ts_utc": [
                ASOF - timedelta(seconds=2),
                ASOF - timedelta(seconds=1),
            ],
            "trade_id": [1, 2],
            "exchange": ["Q", "Q"],
            "price": [10.0, 10.01],
            "size": [100, 200],
            "conditions": [["@"], ["@"]],
            "tape": ["C", "C"],
            "source": ["cloud.alpaca.market_data"] * 2,
            "feed": ["sip"] * 2,
        }
    ).with_columns(pl.col("ts_utc").cast(pl.Datetime("ns", "UTC")))


def _quotes() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["FAST"],
            "ts_utc": [ASOF - timedelta(microseconds=1)],
            "bid_price": [10.0],
            "ask_price": [10.02],
            "bid_size": [100.0],
            "ask_size": [100.0],
            "bid_exchange": ["Q"],
            "ask_exchange": ["P"],
            "conditions": [["R"]],
            "tape": ["C"],
            "source": ["cloud.alpaca.market_data"],
            "feed": ["sip"],
        }
    ).with_columns(pl.col("ts_utc").cast(pl.Datetime("ns", "UTC")))


def test_order_flow_snapshot_is_auditable_and_shadow_only(tmp_path: Path) -> None:
    trade_snapshot, _ = persist_snapshot(
        _trades(),
        root=tmp_path,
        source="raw.alpaca.sip_trades",
        schema_version="sip_trades.v1",
        checks=(),
    )
    quote_snapshot, _ = persist_snapshot(
        _quotes(),
        root=tmp_path,
        source="raw.alpaca.sip_quotes",
        schema_version="sip_quotes.v1",
        checks=(),
    )

    frame, snapshot, path = build_order_flow_snapshot(
        _trades(),
        _quotes(),
        symbols=("FAST", "EMPTY"),
        trade_snapshot=trade_snapshot,
        quote_snapshot=quote_snapshot,
        data_root=tmp_path,
        asof_utc=ASOF,
        window=timedelta(minutes=5),
        provenance="cloud.alpaca.market_data.sip",
    )

    assert snapshot.usable
    assert path.exists()
    assert frame.get_column("symbol").to_list() == ["EMPTY", "FAST"]
    assert frame.get_column("symbol").n_unique() == 2
    assert frame.filter(pl.col("production_eligible")).is_empty()
    assert snapshot.parent_snapshot_ids == (
        trade_snapshot.dataset_id,
        quote_snapshot.dataset_id,
    )
