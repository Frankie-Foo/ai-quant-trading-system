from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl

from data_plane.providers.nasdaq_events import (
    _parse_earnings_payload,
    _parse_trade_halt_xml,
)
from kernel.config import load_config
from kernel.universe import apply_selection_gates, market_cap_tier


def test_nasdaq_earnings_parser_preserves_schedule_and_provenance() -> None:
    retrieved = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
    frame = _parse_earnings_payload(
        {
            "data": {
                "rows": [
                    {
                        "symbol": "FAST",
                        "name": "Fast Corp",
                        "time": "time-after-hours",
                        "marketCap": "$1,234,567,890",
                        "fiscalQuarterEnding": "Jun/2026",
                        "epsForecast": "$0.42",
                        "noOfEsts": "3",
                    }
                ]
            }
        },
        trade_date=date(2026, 7, 20),
        retrieved_utc=retrieved,
    )
    row = frame.row(0, named=True)
    assert row["symbol"] == "FAST"
    assert row["release_timing"] == "after_market"
    assert row["provider_market_cap"] == 1_234_567_890.0
    assert row["retrieved_utc"] == retrieved


def test_nasdaq_halt_parser_handles_luld_and_resumption_times() -> None:
    xml = b"""<?xml version="1.0" encoding="utf-8"?>
    <rss xmlns:ndaq="http://www.nasdaqtrader.com/"><channel><item>
      <title>FAST</title><pubDate>Fri, 17 Jul 2026 04:00:00 GMT</pubDate>
      <ndaq:IssueSymbol>FAST</ndaq:IssueSymbol><ndaq:IssueName>Fast Corp</ndaq:IssueName>
      <ndaq:Mkt>Q</ndaq:Mkt><ndaq:ReasonCode>LUDP</ndaq:ReasonCode>
      <ndaq:HaltDate>07/17/2026</ndaq:HaltDate>
      <ndaq:HaltTime>09:30:17 .163</ndaq:HaltTime>
      <ndaq:ResumptionDate>07/17/2026</ndaq:ResumptionDate>
      <ndaq:ResumptionTradeTime>09:40:17</ndaq:ResumptionTradeTime>
    </item></channel></rss>"""
    frame = _parse_trade_halt_xml(
        xml, retrieved_utc=datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
    )
    row = frame.row(0, named=True)
    assert row["symbol"] == "FAST"
    assert row["is_luld"] is True
    assert row["halt_ts_utc"] == datetime(2026, 7, 17, 13, 30, 17, 163000, tzinfo=UTC)
    assert row["resumption_ts_utc"] == datetime(
        2026, 7, 17, 13, 40, 17, tzinfo=UTC
    )


def test_market_cap_tiers_follow_frozen_config() -> None:
    cfg = load_config("config.yaml")
    assert market_cap_tier(250_000_000_000, cfg) == "mega"
    assert market_cap_tier(15_000_000_000, cfg) == "large"
    assert market_cap_tier(3_000_000_000, cfg) == "mid"
    assert market_cap_tier(500_000_000, cfg) == "small"
    assert market_cap_tier(None, cfg) is None


def _gate_inputs() -> tuple[pl.DataFrame, ...]:
    symbols = ["PASS", "EARN", "HALT", "LULD", "LOWRV", "NOCAP"]
    daily = pl.DataFrame(
        {
            "symbol": symbols,
            "precheck_pass": [True] * 6,
            "reject_reason": ["pending:rvol,market_cap,earnings,luld"] * 6,
            "price": [10.0] * 6,
            "adv_usd": [1_000_000.0] * 6,
            "beta": [2.0] * 6,
            "atr_pct": [0.05] * 6,
        }
    )
    catalysts = pl.DataFrame({"symbol": symbols, "event_count": [1] * 6})
    rvol = pl.DataFrame(
        {
            "symbol": symbols,
            "rvol": [4.0, 5.0, 6.0, 7.0, 3.0, 8.0],
            "rvol_pass": [True, True, True, True, False, True],
            "availability": ["available"] * 6,
            "premarket_open": [10.0] * 6,
            "premarket_high": [11.2] * 6,
            "premarket_low": [9.9] * 6,
            "premarket_close": [11.0] * 6,
            "premarket_vwap": [10.8] * 6,
            "premarket_return": [0.1] * 6,
            "premarket_close_location": [1.0 / 1.3] * 6,
            "premarket_above_vwap": [True] * 6,
            "premarket_price_confirmation": [True] * 6,
        }
    )
    market = pl.DataFrame(
        {
            "symbol": symbols,
            "market_cap": [3e9, 3e9, 3e9, 3e9, 3e9, None],
        }
    )
    earnings = pl.DataFrame(
        {"symbol": ["EARN"], "trade_date": [date(2026, 7, 20)]}
    )
    halts = pl.DataFrame(
        {
            "symbol": ["HALT", "LULD"],
            "halt_date": [date(2026, 7, 20), date(2026, 7, 17)],
            "halt_ts_utc": [
                datetime(2026, 7, 20, 11, 0, tzinfo=UTC),
                datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
            ],
            "resumption_ts_utc": [None, datetime(2026, 7, 17, 14, 5, tzinfo=UTC)],
            "is_luld": [False, True],
        }
    ).with_columns(
        pl.col("halt_ts_utc").cast(pl.Datetime("ms", "UTC")),
        pl.col("resumption_ts_utc").cast(pl.Datetime("ms", "UTC")),
    )
    free_float = pl.DataFrame(
        {
            "symbol": symbols,
            "free_float": [50_000_000, 50_000_000, 50_000_000, 5_000_000, 50_000_000, None],
        }
    )
    return daily, catalysts, rvol, market, earnings, halts, free_float


def test_selection_gates_are_fail_closed_and_rank_only_survivors() -> None:
    cfg = load_config("config.yaml")
    result = apply_selection_gates(
        *_gate_inputs(),
        trade_date=date(2026, 7, 20),
        asof_utc=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        recent_session_dates=[
            date(2026, 7, 13),
            date(2026, 7, 14),
            date(2026, 7, 15),
            date(2026, 7, 16),
            date(2026, 7, 17),
        ],
        cfg=cfg,
        low_float_shares=20_000_000,
    )
    rows = {row["symbol"]: row for row in result.iter_rows(named=True)}
    assert rows["PASS"]["pass_gate"] is True
    assert rows["PASS"]["selection_rank"] == 1
    assert rows["PASS"]["tier"] == "mid"
    assert rows["EARN"]["reject_reason"] == "earnings_day"
    assert rows["HALT"]["reject_reason"] == "current_trading_halt"
    assert rows["LULD"]["reject_reason"] == "recent_luld_low_or_unknown_float"
    assert rows["LOWRV"]["reject_reason"] == "rvol_below_or_equal_min"
    assert rows["NOCAP"]["reject_reason"] == "missing_market_cap"


def test_future_halt_after_asof_does_not_change_current_halt_gate() -> None:
    inputs = list(_gate_inputs())
    halts = inputs[5]
    future = halts.head(1).with_columns(
        pl.lit("PASS").alias("symbol"),
        pl.lit(datetime(2026, 7, 20, 13, 0, tzinfo=UTC))
        .cast(pl.Datetime("ms", "UTC"))
        .alias("halt_ts_utc"),
    )
    inputs[5] = pl.concat([halts, future])
    result = apply_selection_gates(
        *inputs,
        trade_date=date(2026, 7, 20),
        asof_utc=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        recent_session_dates=[date(2026, 7, day) for day in range(13, 18)],
        cfg=load_config("config.yaml"),
        low_float_shares=20_000_000,
    )
    assert result.filter(pl.col("symbol") == "PASS").get_column("pass_gate")[0]


def test_selection_rejects_unsigned_volume_and_negative_gap() -> None:
    inputs = list(_gate_inputs())
    rvol = inputs[2].with_columns(
        pl.when(pl.col("symbol") == "PASS")
        .then(pl.lit(False))
        .otherwise(pl.col("premarket_price_confirmation"))
        .alias("premarket_price_confirmation"),
        pl.when(pl.col("symbol") == "EARN")
        .then(pl.lit(9.9))
        .otherwise(pl.col("premarket_close"))
        .alias("premarket_close"),
    )
    inputs[2] = rvol
    result = apply_selection_gates(
        *inputs,
        trade_date=date(2026, 7, 20),
        asof_utc=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        recent_session_dates=[date(2026, 7, day) for day in range(13, 18)],
        cfg=load_config("config.yaml"),
        low_float_shares=20_000_000,
    )
    rows = {row["symbol"]: row for row in result.iter_rows(named=True)}
    assert (
        rows["PASS"]["reject_reason"]
        == "premarket_volume_not_confirmed_by_price"
    )
    assert "premarket_not_above_prior_close" in rows["EARN"]["reject_reason"]


def test_unresolved_prior_day_halt_is_still_current() -> None:
    inputs = list(_gate_inputs())
    halts = inputs[5]
    unresolved = halts.head(1).with_columns(
        pl.lit("PASS").alias("symbol"),
        pl.lit(date(2026, 7, 17)).alias("halt_date"),
        pl.lit(datetime(2026, 7, 17, 20, 0, tzinfo=UTC))
        .cast(pl.Datetime("ms", "UTC"))
        .alias("halt_ts_utc"),
        pl.lit(None).cast(pl.Datetime("ms", "UTC")).alias("resumption_ts_utc"),
    )
    inputs[5] = pl.concat([halts, unresolved])
    result = apply_selection_gates(
        *inputs,
        trade_date=date(2026, 7, 20),
        asof_utc=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        recent_session_dates=[date(2026, 7, day) for day in range(13, 18)],
        cfg=load_config("config.yaml"),
        low_float_shares=20_000_000,
    )
    row = result.filter(pl.col("symbol") == "PASS").row(0, named=True)
    assert row["current_halt"] is True
    assert row["pass_gate"] is False


def test_empty_locked_pool_is_a_valid_no_selection_session() -> None:
    inputs = list(_gate_inputs())
    inputs[1] = inputs[1].head(0)
    result = apply_selection_gates(
        *inputs,
        trade_date=date(2026, 7, 20),
        asof_utc=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        recent_session_dates=[date(2026, 7, day) for day in range(13, 18)],
        cfg=load_config("config.yaml"),
        low_float_shares=20_000_000,
    )
    assert result.is_empty()
    assert {"pass_gate", "market_cap", "rvol", "gate_asof_utc"}.issubset(
        result.columns
    )
