from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import polars as pl
import pytest

from data_plane.calendar import build_xnys_schedule
from data_plane.quality import canonicalize_bars
from kernel.features.momentum import premarket_window_utc, rvol


def _bar(symbol: str, ts_utc: datetime, volume: int) -> dict[str, object]:
    return {
        "symbol": symbol,
        "ts_utc": ts_utc,
        "open": 10.0,
        "high": 10.0,
        "low": 10.0,
        "close": 10.0,
        "volume": volume,
        "trade_count": 1,
        "vwap": 10.0,
        "source": "test.fixture",
        "feed": "sip",
        "adjustment": "split_adjusted",
    }


def _fixture() -> tuple[pl.DataFrame, pl.DataFrame, date, list[date]]:
    target_date = date(2026, 7, 20)
    schedule = build_xnys_schedule(date(2026, 6, 15), target_date)
    dates = schedule.get_column("trade_date").tail(21).to_list()
    rows: list[dict[str, object]] = []
    for trade_date in dates[:-1]:
        start_utc, _ = premarket_window_utc(trade_date, time(7, 45))
        rows.append(_bar("FAST", start_utc + timedelta(minutes=30), 100))
    target_start, target_end = premarket_window_utc(target_date, time(7, 45))
    rows.extend(
        [
            _bar("FAST", target_start + timedelta(minutes=10), 125),
            _bar("FAST", target_start + timedelta(minutes=100), 175),
            _bar("FAST", target_end, 99_999_999),
            _bar("FAST", target_end + timedelta(hours=3), 99_999_999),
        ]
    )
    return canonicalize_bars(pl.DataFrame(rows)), schedule, target_date, dates


def test_rvol_uses_same_time_history_and_excludes_cutoff_and_later_bars() -> None:
    bars, schedule, target_date, dates = _fixture()
    result = rvol(
        bars,
        schedule=schedule,
        target_date=target_date,
        symbols=("FAST",),
        complete_session_dates=dates,
        cutoff_et=time(7, 45),
        n=20,
        provenance="test.alpaca",
    )
    row = result.row(0, named=True)
    assert row["current_premarket_volume"] == 300
    assert row["median_historical_premarket_volume"] == pytest.approx(100.0)
    assert row["rvol"] == pytest.approx(3.0)
    assert row["rvol_pass"] is False  # frozen gate is strictly greater than three
    assert row["history_session_count"] == 20


def test_future_mutation_cannot_change_rvol() -> None:
    bars, schedule, target_date, dates = _fixture()
    baseline = rvol(
        bars.filter(pl.col("volume") < 99_999_999),
        schedule=schedule,
        target_date=target_date,
        symbols=("FAST",),
        complete_session_dates=dates,
        cutoff_et=time(7, 45),
        n=20,
        provenance="test.alpaca",
    )
    mutated = rvol(
        bars,
        schedule=schedule,
        target_date=target_date,
        symbols=("FAST",),
        complete_session_dates=dates,
        cutoff_et=time(7, 45),
        n=20,
        provenance="test.alpaca",
    )
    assert baseline.to_dicts() == mutated.to_dicts()


def test_successful_no_trade_sessions_are_zero_without_filling_minutes() -> None:
    bars, schedule, target_date, dates = _fixture()
    sparse = bars.filter(
        ~(
            (pl.col("ts_utc") >= datetime(2026, 6, 30, tzinfo=UTC))
            & (pl.col("ts_utc") < datetime(2026, 7, 1, tzinfo=UTC))
        )
    )
    result = rvol(
        sparse,
        schedule=schedule,
        target_date=target_date,
        symbols=("FAST",),
        complete_session_dates=dates,
        cutoff_et=time(7, 45),
        n=20,
        provenance="test.alpaca",
    )
    row = result.row(0, named=True)
    assert row["history_session_count"] == 20
    assert row["historical_nonzero_sessions"] == 19
    assert row["median_historical_premarket_volume"] == pytest.approx(100.0)


def test_incomplete_provider_session_fails_closed() -> None:
    bars, schedule, target_date, dates = _fixture()
    result = rvol(
        bars,
        schedule=schedule,
        target_date=target_date,
        symbols=("FAST",),
        complete_session_dates=dates[:-2] + dates[-1:],
        cutoff_et=time(7, 45),
        n=20,
        provenance="test.alpaca",
    )
    row = result.row(0, named=True)
    assert row["history_session_count"] == 19
    assert row["rvol"] is None
    assert row["rvol_pass"] is False
    assert row["availability"] == "incomplete_history"


def test_zero_historical_median_is_undefined_and_fails_closed() -> None:
    bars, schedule, target_date, dates = _fixture()
    current_only = bars.filter(
        pl.col("ts_utc").dt.convert_time_zone("America/New_York").dt.date()
        == target_date
    )
    result = rvol(
        current_only,
        schedule=schedule,
        target_date=target_date,
        symbols=("FAST",),
        complete_session_dates=dates,
        cutoff_et=time(7, 45),
        n=20,
        provenance="test.alpaca",
    )
    row = result.row(0, named=True)
    assert row["median_historical_premarket_volume"] == 0.0
    assert row["rvol"] is None
    assert row["availability"] == "zero_historical_median"


def test_premarket_window_uses_each_sessions_dst_offset() -> None:
    before_dst, _ = premarket_window_utc(date(2026, 3, 6), time(7, 45))
    after_dst, _ = premarket_window_utc(date(2026, 3, 9), time(7, 45))
    assert before_dst == datetime(2026, 3, 6, 9, 0, tzinfo=UTC)
    assert after_dst == datetime(2026, 3, 9, 8, 0, tzinfo=UTC)


def test_rvol_only_outputs_explicit_locked_symbols() -> None:
    bars, schedule, target_date, dates = _fixture()
    outsider_start, _ = premarket_window_utc(target_date, time(7, 45))
    contaminated = canonicalize_bars(
        pl.concat(
            [
                bars,
                canonicalize_bars(
                    pl.DataFrame([_bar("OUTSIDE", outsider_start, 1_000_000)])
                ),
            ]
        )
    )
    result = rvol(
        contaminated,
        schedule=schedule,
        target_date=target_date,
        symbols=("FAST",),
        complete_session_dates=dates,
        cutoff_et=time(7, 45),
        n=20,
        provenance="test.alpaca",
    )
    assert result.get_column("symbol").to_list() == ["FAST"]
