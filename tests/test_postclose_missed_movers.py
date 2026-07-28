from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest

from research.intraday_selection_postmortem import (
    REVIEW_SCHEMA_VERSION,
    build_intraday_selection_postmortem,
)
from scripts.run_postclose_missed_movers_review import (
    _previous_xnys_session,
    classify_mover,
)

CUTOFF = datetime(2026, 7, 27, 13, 23, tzinfo=UTC)


def test_classify_selected_and_intentional_gate() -> None:
    selected = classify_mover(
        "BX",
        gate_row={"pass_gate": True, "reject_reason": ""},
        news_rows=[],
        selection_cutoff_utc=CUTOFF,
    )
    rejected = classify_mover(
        "ABC",
        gate_row={
            "pass_gate": False,
            "reject_reason": "rvol_below_or_equal_min",
        },
        news_rows=[],
        selection_cutoff_utc=CUTOFF,
    )
    assert selected.category == "selected"
    assert rejected.category == "intentional_gate"
    assert "RVOL" in rejected.detail


def test_classify_late_and_uncaptured_catalysts() -> None:
    before = classify_mover(
        "ABC",
        gate_row=None,
        news_rows=[
            {
                "published_utc": datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
                "headline": "preopen event",
                "symbol_count": 1,
            }
        ],
        selection_cutoff_utc=CUTOFF,
    )
    late = classify_mover(
        "XYZ",
        gate_row=None,
        news_rows=[
            {
                "published_utc": datetime(2026, 7, 27, 15, 0, tzinfo=UTC),
                "headline": "intraday event",
                "symbol_count": 1,
            }
        ],
        selection_cutoff_utc=CUTOFF,
    )
    assert before.category == "data_or_classifier_gap"
    assert late.category == "late_catalyst"


def test_classify_factor_gap_without_news() -> None:
    result = classify_mover(
        "XYZ",
        gate_row=None,
        news_rows=[],
        selection_cutoff_utc=CUTOFF,
    )
    assert result.category == "factor_gap"


def test_broad_mover_roundup_is_not_treated_as_catalyst() -> None:
    result = classify_mover(
        "XYZ",
        gate_row=None,
        news_rows=[
            {
                "published_utc": datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
                "headline": "12 Health Care Stocks Moving In Pre-Market Session",
                "symbol_count": 12,
            }
        ],
        selection_cutoff_utc=CUTOFF,
    )
    assert result.category == "factor_gap"
    assert "价格动量" in result.detail


def _movers() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["SELECTED", "REJECTED", "MISSED"],
            "previous_close": [100.0, 50.0, 20.0],
            "open": [101.0, 51.0, 20.5],
            "high": [110.0, 55.0, 24.0],
            "low": [99.0, 49.0, 19.5],
            "close": [108.0, 54.0, 23.0],
            "volume": [1_000_000, 2_000_000, 3_000_000],
            "close_return": [0.08, 0.08, 0.15],
            "dollar_volume": [108_000_000.0, 108_000_000.0, 69_000_000.0],
            "adv_usd": [80_000_000.0, 50_000_000.0, 30_000_000.0],
            "atr_pct": [0.03, 0.04, 0.05],
        }
    )


def _gates() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["SELECTED", "REJECTED"],
            "pass_gate": [True, False],
            "selection_rank": [1, None],
            "reject_reason": ["", "rvol_below_or_equal_min"],
            "gate_asof_utc": [CUTOFF, CUTOFF],
        }
    ).with_columns(pl.col("gate_asof_utc").cast(pl.Datetime("us", "UTC")))


def test_build_intraday_selection_postmortem_creates_auditable_labels() -> None:
    news = pl.DataFrame(
        {
            "published_utc": [datetime(2026, 7, 27, 12, 0, tzinfo=UTC)],
            "symbols": [["MISSED"]],
            "headline": ["Company announces a material operating update"],
        }
    ).with_columns(pl.col("published_utc").cast(pl.Datetime("us", "UTC")))

    review = build_intraday_selection_postmortem(
        trade_date=date(2026, 7, 27),
        movers=_movers(),
        gates=_gates(),
        news=news,
        news_complete=True,
    )

    assert REVIEW_SCHEMA_VERSION == "intraday_selection_postmortem.v1"
    assert review.get_column("opportunity_rank").to_list() == [1, 2, 3]
    assert review.get_column("symbol").n_unique() == 3
    assert review.get_column("production_change_allowed").to_list() == [False] * 3

    selected = review.filter(pl.col("symbol") == "SELECTED").row(0, named=True)
    rejected = review.filter(pl.col("symbol") == "REJECTED").row(0, named=True)
    missed = review.filter(pl.col("symbol") == "MISSED").row(0, named=True)

    assert selected["decision_outcome"] == "captured_opportunity"
    assert selected["research_eligible"] is False
    assert rejected["root_cause"] == "intentional_gate"
    assert rejected["pattern_key"] == "intentional_gate:rvol_below_or_equal_min"
    assert rejected["research_eligible"] is True
    assert missed["decision_outcome"] == "missed_detectable_opportunity"
    assert missed["root_cause"] == "data_or_classifier_gap"
    assert missed["mfe_from_previous_close"] == pytest.approx(0.20)
    assert missed["mae_from_previous_close"] == pytest.approx(-0.025)
    assert "MISSED" not in missed["pattern_key"]


def test_postmortem_marks_missing_news_as_incomplete_instead_of_inventing_a_gap() -> None:
    empty_news = pl.DataFrame(
        schema={
            "published_utc": pl.Datetime("us", "UTC"),
            "symbols": pl.List(pl.String),
            "headline": pl.String,
        }
    )
    review = build_intraday_selection_postmortem(
        trade_date=date(2026, 7, 27),
        movers=_movers().filter(pl.col("symbol") == "MISSED"),
        gates=_gates(),
        news=empty_news,
        news_complete=False,
    )
    row = review.row(0, named=True)

    assert row["root_cause"] == "incomplete_evidence"
    assert row["decision_outcome"] == "incomplete_evidence"
    assert row["research_eligible"] is False


def test_postmortem_rejects_duplicate_opportunities() -> None:
    duplicates = pl.concat([_movers().head(1), _movers().head(1)])
    with pytest.raises(ValueError, match="duplicate symbols"):
        build_intraday_selection_postmortem(
            trade_date=date(2026, 7, 27),
            movers=duplicates,
            gates=_gates(),
            news=pl.DataFrame(),
            news_complete=True,
        )


def test_previous_universe_date_uses_exchange_calendar_not_hardcoded_days() -> None:
    assert _previous_xnys_session(date(2026, 7, 27)) == date(2026, 7, 24)
    assert _previous_xnys_session(date(2026, 7, 28)) == date(2026, 7, 27)
