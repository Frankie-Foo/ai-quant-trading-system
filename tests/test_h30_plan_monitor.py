from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from scripts.monitor_h30_plan import ADD_VOLUME_RATIO, ADD_VOLUME_THRESHOLDS, ACTIVE_SYMBOLS, CANDIDATE_PLANS, FALLBACK_PLANS, FALLBACK_WATCHLIST, MARKET_PROXIES, MAX_BOX_WIDTH, PAPER_REENTRY_NOTIONAL, PAPER_REENTRY_RISK, PLANS, PORTFOLIO_NOTIONAL_LIMIT, PORTFOLIO_RISK_LIMIT, SCOUT_VOLUME_RATIO, STORAGE_PEERS, _completed_fives, _daily_trend_clear, _scout_trigger, _verification_covers_candidates, _verified_symbols, h30_box


OPEN = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)


def test_h30_box_requires_six_complete_five_minute_bars() -> None:
    rows = []
    for minute in range(30):
        rows.append(
            {
                "ts_utc": OPEN + timedelta(minutes=minute),
                "high": 101 + minute / 100,
                "low": 99 + minute / 100,
                "close": 100 + minute / 100,
                "volume": 100,
            }
        )
    box = h30_box(pl.DataFrame(rows), market_open_utc=OPEN, now_utc=OPEN + timedelta(minutes=30))
    assert box == pytest.approx((101.29, 99.0, 100.165, 500.0))


def test_scout_and_add_volume_thresholds_are_separate() -> None:
    assert SCOUT_VOLUME_RATIO == 0.8
    assert ADD_VOLUME_RATIO == 1.5
    assert MAX_BOX_WIDTH == 0.05
    assert MARKET_PROXIES == ("SPY", "QQQ")
    assert STORAGE_PEERS == ("MU", "WDC")
    assert FALLBACK_WATCHLIST == ("REZI", "ALAB", "CAE", "WDC", "MU")
    assert [plan.sector_proxy for plan in PLANS] == ["ITA", "SMH", "IGV"]
    assert [plan.symbol for plan in FALLBACK_PLANS] == ["REZI", "ALAB", "CAE", "WDC"]
    assert len(CANDIDATE_PLANS) == 7
    assert ACTIVE_SYMBOLS == ("SNDK", "WDC")
    assert PORTFOLIO_NOTIONAL_LIMIT == 2_000_000.0
    assert PORTFOLIO_RISK_LIMIT == 30_000.0
    assert ADD_VOLUME_THRESHOLDS == (1.0, 1.25, 1.5)
    assert PAPER_REENTRY_NOTIONAL == 80_000.0
    assert PAPER_REENTRY_RISK == 5_000.0


def test_scout_trigger_uses_two_one_minute_closes_and_three_nonlower_lows() -> None:
    rows = []
    for minute in range(32):
        rows.append({"ts_utc": OPEN + timedelta(minutes=minute), "low": 99 + max(0, minute - 29), "close": 100 + minute / 10, "volume": 1000})
    bars = pl.DataFrame(rows)
    stop = _scout_trigger(bars, high=102.9, vwap=100.0, market_open_utc=OPEN, now_utc=OPEN + timedelta(minutes=32))
    assert stop == pytest.approx((99.0, 1000.0))


def test_completed_fives_require_five_observed_one_minute_bars() -> None:
    rows = [{"ts_utc": OPEN + timedelta(minutes=i), "high": 101, "low": 99, "close": 100, "volume": 100} for i in range(10)]
    assert len(_completed_fives(pl.DataFrame(rows), market_open_utc=OPEN, now_utc=OPEN + timedelta(minutes=10))) == 2


def test_daily_trend_requires_completed_higher_highs_lows_and_close_above_sma20() -> None:
    rows = [
        {"t": (OPEN - timedelta(days=30 - index)).isoformat(), "c": 100 + index, "h": 101 + index, "l": 99 + index}
        for index in range(20)
    ]
    passed, values = _daily_trend_clear(rows, session_date=OPEN)
    assert passed is True
    assert values["last_close"] > values["sma20"]


def test_daily_verification_allows_each_passing_symbol_independently(tmp_path) -> None:
    path = tmp_path / "daily.json"
    path.write_text('{"symbols":{"HAWK":{"passed":false},"SNDK":{"passed":true}}}', encoding="utf-8")
    assert _verified_symbols(path) == {"SNDK"}
    assert _verification_covers_candidates(path) is False
