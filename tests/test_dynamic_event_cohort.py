from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import polars as pl

from data_plane.contracts import EventObservation
from data_plane.event_ledger import EventLedger
from research.event_cohort import build_dynamic_event_cohort


def _event(
    event_id: str,
    *,
    symbols: tuple[str, ...],
    source: str = "wire.one",
    headline: str = "Issuer wins contract",
    summary: str = "Material multi-year award",
    first_seen_at: datetime | None = datetime(2026, 9, 14, 9, 55, tzinfo=UTC),
    origin: Literal["forward", "historical", "manual"] = "forward",
) -> EventObservation:
    return EventObservation(
        source=source,
        source_event_id=event_id,
        symbols=symbols,
        headline=headline,
        summary=summary,
        published_at=datetime(2026, 9, 14, 9, 0, tzinfo=UTC),
        first_seen_at=first_seen_at,
        source_url=f"https://example.test/{event_id}",
        origin=origin,
    )


def test_dynamic_cohort_keeps_all_symbols_and_marks_missing_trade_evidence(
    tmp_path: Path,
) -> None:
    symbols = tuple(f"S{index:02d}" for index in range(12))
    clocks = iter(
        (
            datetime(2026, 9, 14, 10, 0, tzinfo=UTC),
            datetime(2026, 9, 14, 10, 5, tzinfo=UTC),
            datetime(2026, 9, 14, 10, 6, tzinfo=UTC),
        )
    )
    with EventLedger(tmp_path / "events.sqlite3", clock=lambda: next(clocks)) as ledger:
        ledger.append(_event("forward-1", symbols=symbols))
        ledger.append(
            _event(
                "historical-1",
                symbols=("HIST",),
                first_seen_at=None,
                origin="historical",
            )
        )
        ledger.append(
            _event("forward-copy", source="wire.two", symbols=tuple(reversed(symbols)))
        )
        cohort = build_dynamic_event_cohort(
            ledger, asof_utc=datetime(2026, 9, 14, 10, 6, tzinfo=UTC)
        )

    assert cohort.height == 25
    assert set(symbols) <= set(cohort.get_column("symbol"))
    assert cohort.filter(pl.col("symbol") == "HIST").row(0, named=True)["coverage"] == (
        "research_only"
    )
    assert cohort.get_column("market_cap").null_count() == 25
    assert cohort.get_column("outcome_label").null_count() == 25
    assert cohort.get_column("tradable").to_list() == [False] * 25
    assert cohort.get_column("is_cluster_representative").sum() == 13
    assert all(
        "market_cap_missing" in reason
        for reason in cohort.get_column("missing_reason").to_list()
    )


def test_dynamic_cohort_adds_intraday_symbols_only_after_they_are_available(
    tmp_path: Path,
) -> None:
    clocks = iter(
        (
            datetime(2026, 9, 14, 10, 0, tzinfo=UTC),
            datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
        )
    )
    with EventLedger(tmp_path / "events.sqlite3", clock=lambda: next(clocks)) as ledger:
        ledger.append(_event("morning", symbols=("EARLY",)))
        ledger.append(_event("midday", symbols=("NEW",)))
        morning = build_dynamic_event_cohort(
            ledger, asof_utc=datetime(2026, 9, 14, 11, 0, tzinfo=UTC)
        )
        midday = build_dynamic_event_cohort(
            ledger, asof_utc=datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
        )

    assert morning.get_column("symbol").to_list() == ["EARLY"]
    assert midday.get_column("symbol").to_list() == ["EARLY", "NEW"]


def test_dynamic_cohort_empty_result_has_stable_public_schema(tmp_path: Path) -> None:
    with EventLedger(tmp_path / "events.sqlite3") as ledger:
        cohort = build_dynamic_event_cohort(
            ledger, asof_utc=datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
        )

    assert cohort.is_empty()
    assert cohort.schema["available_at"] == pl.Datetime("us", "UTC")
    assert cohort.schema["market_cap"] == pl.Float64
    assert cohort.schema["tradable"] == pl.Boolean
