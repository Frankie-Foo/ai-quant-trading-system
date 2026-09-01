from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.catalysts import canonicalize_catalysts
from kernel.catalysts import prepare_catalysts
from research.event_cohort import build_event_cohort


def test_event_cohort_uses_current_2000_beijing_lock() -> None:
    published = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
    news = canonicalize_catalysts(
        pl.DataFrame(
            [
                {
                    "source": "massive.news",
                    "source_event_id": "morning-contract",
                    "event_type": "news",
                    "event_subtype": None,
                    "published_utc": published,
                    "updated_utc": None,
                    "retrieved_utc": published,
                    "symbols": ["FAST"],
                    "headline": "Fast signs a material multi-year customer contract",
                    "summary": (
                        "The company announced a material customer contract with "
                        "specific commercial terms, a multi-year duration, committed "
                        "customer volumes, expected revenue, implementation dates, "
                        "and independently verifiable counterpart details."
                    ),
                    "publisher": "Newswire",
                    "url": "https://example.test/contract",
                    "cik": None,
                    "accession_number": None,
                    "form_items": [],
                    "tags": ["contracts"],
                    "provenance": "massive.news:morning-contract",
                },
                {
                    "source": "alpaca.news.benzinga",
                    "source_event_id": "morning-contract-copy",
                    "event_type": "news",
                    "event_subtype": None,
                    "published_utc": published,
                    "updated_utc": None,
                    "retrieved_utc": published,
                    "symbols": ["FAST"],
                    "headline": "Fast signs a material multi-year customer contract",
                    "summary": (
                        "The company announced a material customer contract with "
                        "specific commercial terms, a multi-year duration, committed "
                        "customer volumes, expected revenue, implementation dates, "
                        "and independently verifiable counterpart details."
                    ),
                    "publisher": "Benzinga",
                    "url": "https://example.test/contract-copy",
                    "cik": None,
                    "accession_number": None,
                    "form_items": [],
                    "tags": ["contracts"],
                    "provenance": "alpaca.news:morning-contract-copy",
                },
            ]
        )
    )
    prepared = prepare_catalysts(
        news,
        asof_utc=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    )

    cohort = build_event_cohort(
        prepared,
        schedule=build_xnys_schedule(date(2026, 7, 17), date(2026, 7, 20)),
        target_dates=(date(2026, 7, 20),),
    )

    assert cohort.get_column("symbol").to_list() == ["FAST"]
    assert cohort.row(0, named=True)["latest_event_utc"] == published
