from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl

from data_plane.calendar import build_xnys_schedule
from execution.alpaca_paper import PaperPosition
from kernel.adaptive_trade_plan import BaselineTradePlan, PlanMode
from operations.adaptive_plan_adapters import (
    CloudBrokerPositionAdapter,
    PlanEvidence,
    SipStoreMarketFactsAdapter,
)

TRADE_DATE = date(2026, 7, 28)
OBSERVED = datetime(2026, 7, 28, 16, 0, 30, tzinfo=UTC)


def _plan() -> BaselineTradePlan:
    return BaselineTradePlan(
        plan_id="plan-20260728-XYZ",
        symbol="XYZ",
        trade_date=TRADE_DATE,
        mode=PlanMode.CATALYST,
        entry_window_end_utc=datetime(2026, 7, 28, 17, 30, tzinfo=UTC),
        force_exit_utc=datetime(2026, 7, 28, 19, 55, tzinfo=UTC),
        hard_stop=99.0,
        max_risk_dollars=300.0,
        max_notional=20_000.0,
        probe_fraction=0.25,
        max_spread_ratio=0.0025,
        soft_cooldown=timedelta(minutes=3),
        max_soft_revisions=3,
    )


def _bars(symbol: str, slope: float) -> pl.DataFrame:
    schedule = build_xnys_schedule(date(2026, 7, 27), TRADE_DATE)
    rows: list[dict[str, object]] = []
    index = 0
    for session in schedule.iter_rows(named=True):
        cursor = session["market_open_utc"]
        close = session["market_close_utc"]
        assert isinstance(cursor, datetime)
        assert isinstance(close, datetime)
        while cursor < min(close, OBSERVED):
            price = 100.0 + index * slope
            rows.append(
                {
                    "symbol": symbol,
                    "ts_utc": cursor,
                    "open": price - slope * 0.5,
                    "high": price + 0.03,
                    "low": price - 0.03,
                    "close": price,
                    "vwap": price - 0.01,
                    "volume": 2_000 if cursor == OBSERVED.replace(
                        second=0, microsecond=0
                    ) - timedelta(minutes=1) else 1_000,
                    "trade_count": 20,
                }
            )
            cursor += timedelta(minutes=1)
            index += 1
    return pl.DataFrame(rows)


def _quotes(symbol: str) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [symbol, symbol],
            "ts_utc": [
                OBSERVED - timedelta(seconds=2),
                OBSERVED - timedelta(seconds=1),
            ],
            "bid_price": [106.98, 106.99],
            "ask_price": [107.00, 107.01],
            "bid_size": [500, 600],
            "ask_size": [300, 250],
            "source": ["alpaca", "alpaca"],
            "feed": ["sip", "sip"],
        }
    )


def _trades(symbol: str) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for index in range(12):
        rows.append(
            {
                "symbol": symbol,
                "ts_utc": OBSERVED - timedelta(seconds=20 - index),
                "trade_id": index + 1,
                "exchange": "Q",
                "price": 106.80 + index * 0.01,
                "size": 100,
                "conditions": ["@"],
                "tape": "C",
                "source": "alpaca",
                "feed": "sip",
            }
        )
    return pl.DataFrame(rows)


class FakeSipStore:
    def __init__(self) -> None:
        self.bar_frames = {
            "XYZ": _bars("XYZ", 0.010),
            "SPY": _bars("SPY", 0.004),
            "XLK": _bars("XLK", 0.006),
        }

    def bars_for_symbol(
        self,
        symbol: str,
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> pl.DataFrame:
        return self.bar_frames[symbol].filter(
            (pl.col("ts_utc") >= start_utc) & (pl.col("ts_utc") < end_utc)
        )

    def quotes_for_symbol(
        self,
        symbol: str,
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> pl.DataFrame:
        return _quotes(symbol).filter(
            (pl.col("ts_utc") >= start_utc) & (pl.col("ts_utc") < end_utc)
        )

    def trades_for_symbol(
        self,
        symbol: str,
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> pl.DataFrame:
        return _trades(symbol).filter(
            (pl.col("ts_utc") >= start_utc) & (pl.col("ts_utc") < end_utc)
        )


def test_sip_adapter_builds_multitimeframe_market_and_order_flow_facts() -> None:
    adapter = SipStoreMarketFactsAdapter(
        store=FakeSipStore(),
        evidence={
            _plan().plan_id: PlanEvidence(
                benchmark_symbol="SPY",
                sector_symbol="XLK",
                catalyst_score=0.82,
                provenance="accepted.selection@test",
            )
        },
    )

    facts = adapter.read(_plan(), observed_at_utc=OBSERVED)

    assert facts.data_complete is True
    assert facts.completed_one_minute_bar_utc is not None
    assert facts.completed_one_minute_bar_utc <= OBSERVED
    assert facts.five_minute_confirmed is True
    assert facts.fifteen_minute_confirmed is True
    assert facts.benchmark_above_vwap is True
    assert facts.sector_above_vwap is True
    assert facts.relative_strength is not None and facts.relative_strength > 0
    assert facts.order_flow_imbalance is not None
    assert facts.order_flow_imbalance > 0.20
    assert facts.catalyst_score == 0.82


class FakePaperBroker:
    def __init__(self, positions: tuple[PaperPosition, ...]):
        self.positions = positions

    def list_positions(self) -> tuple[PaperPosition, ...]:
        return self.positions


def test_cloud_broker_adapter_treats_broker_as_position_authority() -> None:
    adapter = CloudBrokerPositionAdapter(
        FakePaperBroker(
            (
                PaperPosition(
                    symbol="XYZ",
                    qty="50",
                    side="long",
                    market_value="5350",
                    avg_entry_price="101.25",
                    current_price="107.00",
                ),
            )
        )
    )

    position = adapter.position("XYZ", observed_at_utc=OBSERVED)
    flat = adapter.position("NONE", observed_at_utc=OBSERVED)

    assert position is not None
    assert position.shares == 50
    assert position.average_entry == 101.25
    assert flat is None
