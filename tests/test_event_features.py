from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from kernel.event_features import (
    FeatureSnapshot,
    ResearchSession,
    adapt_start_stamped_bars,
    build_event_features,
)

OPEN = datetime(2026, 9, 14, 13, 30, tzinfo=UTC)
SESSION = ResearchSession(
    session_id="2026-09-14",
    open_utc=OPEN,
    close_utc=datetime(2026, 9, 14, 20, 0, tzinfo=UTC),
)


def _bars(symbol: str, count: int, *, base: float = 100.0) -> list[dict[str, object]]:
    rows = []
    for index in range(count):
        start = OPEN + timedelta(minutes=index)
        rows.append(
            {
                "symbol": symbol,
                "session_id": SESSION.session_id,
                "source": "test.market",
                "feed": "sip",
                "price_basis": "split",
                "bar_start_utc": start,
                "bar_end_utc": start + timedelta(minutes=1),
                "available_at": start + timedelta(minutes=1),
                "open": base + index,
                "high": base + index + 2,
                "low": base + index - 1,
                "close": base + index + 1,
                "volume": 10 * (index + 1),
                "vwap": base + index + 0.5,
            }
        )
    return rows


def _frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(rows).with_columns(
        pl.col("bar_start_utc", "bar_end_utc", "available_at").cast(
            pl.Datetime("us", "UTC")
        )
    )


def _snapshot(
    bars: pl.DataFrame,
    *,
    decision_at: datetime,
    sector_symbol: str | None = None,
    index_symbol: str | None = None,
    sector_mapping_available_at: datetime | None = None,
) -> FeatureSnapshot:
    return build_event_features(
        bars,
        symbol="TEST",
        session=SESSION,
        decision_at=decision_at,
        source="test.market",
        feed="sip",
        price_basis="split",
        interval=timedelta(minutes=1),
        sector_symbol=sector_symbol,
        index_symbol=index_symbol,
        sector_mapping_available_at=sector_mapping_available_at,
    )


def test_complete_bars_produce_hand_calculated_opening_and_vwap_features() -> None:
    snapshot = _snapshot(
        _frame(_bars("TEST", 30)),
        decision_at=OPEN + timedelta(minutes=30),
    )

    assert snapshot.completed_through_utc == OPEN + timedelta(minutes=30)
    assert snapshot.features["h15_high"].value == 116.0
    assert snapshot.features["h15_low"].value == 99.0
    assert snapshot.features["h30_high"].value == 131.0
    assert snapshot.features["h30_low"].value == 99.0
    assert snapshot.features["session_vwap"].value == pytest.approx(119.8333333333)
    assert snapshot.features["vwap_slope_per_minute"].value == pytest.approx(2 / 3)
    assert snapshot.features["session_volume"].value == 4650
    assert snapshot.features["session_dollar_volume"].value == pytest.approx(557225)
    assert snapshot.features["higher_high"].value is True
    assert snapshot.features["higher_low"].value is True
    assert snapshot.features["last_to_previous_volume"].value == pytest.approx(300 / 290)


def test_unfinished_and_late_bars_cannot_change_an_earlier_snapshot() -> None:
    decision = OPEN + timedelta(minutes=15)
    baseline = _frame(_bars("TEST", 15))
    future = _bars("TEST", 16)[-1]
    future["open"] = 1_000.0
    future["high"] = 2_000.0
    future["low"] = 900.0
    future["close"] = 1_500.0
    late_revision = dict(_bars("TEST", 15)[-1])
    late_revision["available_at"] = decision + timedelta(seconds=1)
    late_revision["high"] = 9_999.0

    expected = _snapshot(baseline, decision_at=decision)
    observed = _snapshot(
        _frame([*baseline.to_dicts(), future, late_revision]),
        decision_at=decision,
    )

    assert observed == expected
    assert observed.features["h15_high"].value == 116.0


def test_relative_returns_use_matching_stock_sector_and_index_windows() -> None:
    rows = []
    for symbol, final_close in (("TEST", 110.0), ("XLK", 105.0), ("QQQ", 102.0)):
        symbol_rows = _bars(symbol, 2)
        symbol_rows[-1]["close"] = final_close
        symbol_rows[-1]["high"] = final_close + 1
        rows.extend(symbol_rows)

    snapshot = _snapshot(
        _frame(rows),
        decision_at=OPEN + timedelta(minutes=2),
        sector_symbol="XLK",
        index_symbol="QQQ",
        sector_mapping_available_at=OPEN,
    )

    assert snapshot.features["stock_minus_sector_return"].value == pytest.approx(0.05)
    assert snapshot.features["sector_minus_index_return"].value == pytest.approx(0.03)


def test_start_stamped_adapter_requires_explicit_availability() -> None:
    bars = pl.DataFrame(
        {
            "ts_utc": [OPEN],
            "available_at": [OPEN + timedelta(minutes=1)],
        }
    ).with_columns(pl.all().cast(pl.Datetime("us", "UTC")))

    adapted = adapt_start_stamped_bars(bars, interval=timedelta(minutes=1))
    assert adapted.row(0, named=True)["bar_start_utc"] == OPEN
    assert adapted.row(0, named=True)["bar_end_utc"] == OPEN + timedelta(minutes=1)

    with pytest.raises(ValueError, match="available_at"):
        adapt_start_stamped_bars(
            bars.drop("available_at"), interval=timedelta(minutes=1)
        )
