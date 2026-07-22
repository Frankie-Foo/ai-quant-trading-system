from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from data_plane.calendar import build_xnys_schedule
from data_plane.daily import DAILY_COLUMNS, audit_daily_bars, canonicalize_daily_bars
from data_plane.http import _safe_url
from data_plane.providers.alpaca import credentials_from_env, stock_data_policy_from_env
from data_plane.providers.massive import (
    _set_query_value,
    api_key_from_env,
    empty_ticker_details_frame,
)
from data_plane.quality import BAR_COLUMNS, BAR_SCHEMA_VERSION, audit_minute_bars, canonicalize_bars
from data_plane.sessions import filter_rth, minute_coverage
from data_plane.storage import persist_snapshot, sha256_file

NOW = datetime(2026, 7, 16, 13, 30, tzinfo=UTC)


def _bars() -> pl.DataFrame:
    return canonicalize_bars(
        pl.DataFrame(
            {
                "symbol": ["AAPL", "AAPL"],
                "ts_utc": [NOW, NOW.replace(minute=31)],
                "open": [210.0, 210.5],
                "high": [211.0, 211.2],
                "low": [209.8, 210.4],
                "close": [210.5, 211.0],
                "volume": [1000, 1200],
                "trade_count": [20, 25],
                "vwap": [210.4, 210.9],
                "source": ["test.fixture", "test.fixture"],
                "feed": ["sip", "sip"],
                "adjustment": ["raw", "raw"],
            }
        )
    )


def test_canonical_minute_bars_pass_quality_gate() -> None:
    frame = _bars()
    checks = audit_minute_bars(
        frame,
        provenance="test.fixture",
        expected_symbols=("AAPL",),
        research_approved=True,
    )
    assert tuple(frame.columns) == BAR_COLUMNS
    assert all(check.passed for check in checks)


def test_canonicalizes_rfc3339_provider_timestamps() -> None:
    frame = _bars().with_columns(pl.col("ts_utc").dt.to_string("%Y-%m-%dT%H:%M:%SZ"))
    normalized = canonicalize_bars(frame)
    assert normalized.schema["ts_utc"] == pl.Datetime("ms", "UTC")
    assert normalized.get_column("ts_utc")[0] == NOW


def test_unverified_community_source_is_quarantined(tmp_path: Path) -> None:
    frame = _bars()
    checks = audit_minute_bars(
        frame,
        provenance="test.community",
        expected_symbols=("AAPL",),
        research_approved=False,
    )
    snapshot, path = persist_snapshot(
        frame,
        root=tmp_path,
        source="community.test",
        schema_version=BAR_SCHEMA_VERSION,
        checks=checks,
    )
    assert snapshot.usable is False
    assert path.parent.parent.name == "quarantine"
    assert sha256_file(path) == snapshot.content_sha256
    assert (path.parent / "manifest.json").exists()


def test_duplicate_and_bad_ohlc_are_critical_failures() -> None:
    frame = _bars().with_columns(
        pl.when(pl.int_range(pl.len()) == 1)
        .then(pl.lit(NOW))
        .otherwise(pl.col("ts_utc"))
        .alias("ts_utc"),
        pl.when(pl.int_range(pl.len()) == 1)
        .then(pl.lit(209.0))
        .otherwise(pl.col("high"))
        .alias("high"),
    )
    checks = audit_minute_bars(
        frame,
        provenance="test.invalid",
        expected_symbols=("AAPL",),
        research_approved=True,
    )
    failed = {check.name for check in checks if not check.passed}
    assert "unique_symbol_timestamp" in failed
    assert "ohlc_logic" in failed


def test_provider_credentials_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ALPACA_API_KEY_ID"):
        credentials_from_env()
    with pytest.raises(RuntimeError, match="MASSIVE_API_KEY"):
        api_key_from_env()


def test_alpaca_stock_policy_uses_realtime_sip_without_a_synthetic_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALPACA_MARKET_DATA_FEED", raising=False)
    realtime = stock_data_policy_from_env()
    assert realtime.feed == "sip"
    assert realtime.delay_minutes == 0
    assert realtime.is_realtime is True

    monkeypatch.setenv("ALPACA_MARKET_DATA_FEED", "delayed_sip")
    delayed = stock_data_policy_from_env()
    assert delayed.feed == "delayed_sip"
    assert delayed.delay_minutes == 15
    assert delayed.is_realtime is False


def test_alpaca_stock_policy_rejects_unknown_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPACA_MARKET_DATA_FEED", "iex")
    with pytest.raises(RuntimeError, match="ALPACA_MARKET_DATA_FEED"):
        stock_data_policy_from_env()


def test_xnys_calendar_preserves_early_close_and_utc() -> None:
    schedule = build_xnys_schedule(date(2025, 7, 1), date(2025, 7, 5))
    july_third = schedule.filter(pl.col("trade_date") == date(2025, 7, 3)).row(
        0, named=True
    )
    assert july_third["session_minutes"] == 210
    assert july_third["is_half_day"] is True
    assert str(schedule.schema["market_open_utc"]) == (
        "Datetime(time_unit='ms', time_zone='UTC')"
    )


def test_rth_filter_uses_calendar_and_does_not_fill_missing_minutes() -> None:
    schedule = build_xnys_schedule(date(2025, 7, 3), date(2025, 7, 3))
    bars = canonicalize_bars(
        pl.DataFrame(
            {
                "symbol": ["AAPL", "AAPL", "AAPL"],
                "ts_utc": [
                    datetime(2025, 7, 3, 13, 29, tzinfo=UTC),
                    datetime(2025, 7, 3, 13, 30, tzinfo=UTC),
                    datetime(2025, 7, 3, 13, 32, tzinfo=UTC),
                ],
                "open": [210.0, 210.0, 210.5],
                "high": [211.0, 211.0, 211.0],
                "low": [209.8, 209.8, 210.4],
                "close": [210.5, 210.5, 210.8],
                "volume": [100, 1000, 1200],
                "trade_count": [2, 20, 25],
                "vwap": [210.4, 210.4, 210.7],
                "source": ["test.fixture"] * 3,
                "feed": ["sip"] * 3,
                "adjustment": ["split_dividend"] * 3,
            }
        )
    )
    rth = filter_rth(bars, schedule)
    coverage = minute_coverage(bars, schedule)
    assert rth.height == 2
    assert coverage.row(0, named=True)["session_minutes"] == 210
    assert coverage.row(0, named=True)["missing_minutes"] == 208


def test_canonical_daily_bars_pass_quality_gate() -> None:
    trade_date = date(2025, 1, 2)
    frame = canonicalize_daily_bars(
        pl.DataFrame(
            {
                "symbol": ["AAPL"],
                "trade_date": [trade_date],
                "provider_ts_utc": [datetime(2025, 1, 2, 21, 0, tzinfo=UTC)],
                "open": [248.93],
                "high": [249.10],
                "low": [241.82],
                "close": [243.85],
                "volume": [55_705_120.5],
                "trade_count": [672_000],
                "vwap": [244.91],
                "source": ["massive.grouped_daily"],
                "feed": ["sip_excluding_otc"],
                "adjustment": ["split_adjusted"],
            }
        )
    )
    checks = audit_daily_bars(
        frame,
        provenance="test.massive",
        expected_date=trade_date,
    )
    assert tuple(frame.columns) == DAILY_COLUMNS
    assert all(check.passed for check in checks)


def test_error_url_never_retains_query_credentials() -> None:
    safe = _safe_url("https://api.example.test/path?apiKey=secret&cursor=abc")
    assert safe == "https://api.example.test/path"


def test_massive_pagination_keeps_cursor_when_reasserting_page_size() -> None:
    url = _set_query_value("https://api.example.test/path?cursor=opaque", "limit", "1000")
    assert "cursor=opaque" in url
    assert "limit=1000" in url


def test_empty_ticker_details_has_stable_point_in_time_schema() -> None:
    frame = empty_ticker_details_frame()
    assert frame.is_empty()
    assert frame.schema["asof_date"] == pl.Date
    assert frame.schema["market_cap"] == pl.Float64
    assert frame.schema["retrieved_utc"] == pl.Datetime("ms", "UTC")
