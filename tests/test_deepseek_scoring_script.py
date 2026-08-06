from __future__ import annotations

from datetime import UTC, datetime

import polars as pl
import pytest

from scripts.score_catalysts_deepseek import build_symbol_evidence


def test_build_symbol_evidence_uses_only_locked_event_ids() -> None:
    candidates = pl.DataFrame(
        {
            "symbol": ["FAST"],
            "evidence_event_ids": [["news:1", "sec:2"]],
        }
    )
    prepared = pl.DataFrame(
        {
            "source": ["news", "sec", "news"],
            "source_event_id": ["1", "2", "future"],
            "published_utc": [
                datetime(2026, 7, 17, 20, tzinfo=UTC),
                datetime(2026, 7, 17, 21, tzinfo=UTC),
                datetime(2026, 7, 20, 1, tzinfo=UTC),
            ],
            "event_type": ["news", "sec_filing", "news"],
            "event_subtype": [None, "8-K", None],
            "headline": ["Material contract won", None, "Future headline"],
            "summary": ["Multi-year customer award", None, "Must not be used"],
            "form_items": [[], ["1.01"], []],
            "catalyst_category": [
                "contract_partnership",
                "contract_partnership",
                "general_news",
            ],
        }
    ).with_columns(pl.col("published_utc").cast(pl.Datetime("ms", "UTC")))
    result = build_symbol_evidence(
        candidates,
        prepared,
        asof_utc=datetime(2026, 7, 20, 0, tzinfo=UTC),
    )
    assert [event_id for event_id, _ in result["FAST"]] == ["news:1", "sec:2"]
    assert "Material contract won" in result["FAST"][0][1]
    assert "8-K" in result["FAST"][1][1]
    assert "Future headline" not in str(result)


def test_build_symbol_evidence_fails_when_locked_evidence_is_missing() -> None:
    candidates = pl.DataFrame(
        {"symbol": ["FAST"], "evidence_event_ids": [["news:missing"]]}
    )
    prepared = pl.DataFrame(
        schema={
            "source": pl.String,
            "source_event_id": pl.String,
            "published_utc": pl.Datetime("ms", "UTC"),
            "event_type": pl.String,
            "event_subtype": pl.String,
            "headline": pl.String,
            "summary": pl.String,
            "form_items": pl.List(pl.String),
            "catalyst_category": pl.String,
        }
    )
    with pytest.raises(ValueError, match="missing locked evidence"):
        build_symbol_evidence(
            candidates,
            prepared,
            asof_utc=datetime(2026, 7, 20, 0, tzinfo=UTC),
        )
