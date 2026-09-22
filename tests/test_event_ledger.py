from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import polars as pl
import pytest
from pydantic import ValidationError

from data_plane.catalysts import canonicalize_catalysts
from data_plane.contracts import EventObservation
from data_plane.event_ledger import EventLedger
from data_plane.providers.catalyst_news import ingest_catalyst_events


def _observation(
    *,
    source: str = "wire.one",
    source_event_id: str = "event-1",
    headline: str = "Issuer wins contract",
    summary: str = "Material multi-year award",
    published_at: datetime = datetime(2026, 9, 14, 9, 0, tzinfo=UTC),
    updated_at: datetime | None = None,
    first_seen_at: datetime | None = datetime(2026, 9, 14, 10, 10, tzinfo=UTC),
    origin: Literal["forward", "historical", "manual"] = "forward",
) -> EventObservation:
    return EventObservation(
        source=source,
        source_event_id=source_event_id,
        symbols=("TEST",),
        headline=headline,
        summary=summary,
        published_at=published_at,
        updated_at=updated_at,
        first_seen_at=first_seen_at,
        source_url=f"https://example.test/{source_event_id}",
        origin=origin,
        provenance=f"{source}:{source_event_id}",
    )


def _canonical_frame() -> pl.DataFrame:
    return canonicalize_catalysts(
        pl.DataFrame(
            [
                {
                    "source": "alpaca.news.benzinga",
                    "source_event_id": "news-1",
                    "event_type": "news",
                    "event_subtype": None,
                    "published_utc": datetime(2026, 9, 14, 9, 0, tzinfo=UTC),
                    "updated_utc": datetime(2026, 9, 14, 9, 30, tzinfo=UTC),
                    "retrieved_utc": datetime(2026, 9, 14, 10, 10, tzinfo=UTC),
                    "symbols": ["TEST"],
                    "headline": "Issuer wins contract",
                    "summary": "Material multi-year award",
                    "publisher": "Benzinga",
                    "url": "https://example.test/news-1",
                    "cik": None,
                    "accession_number": None,
                    "form_items": [],
                    "tags": ["contract"],
                    "provenance": "alpaca.news:news-1",
                }
            ]
        )
    )


def test_restart_and_duplicate_import_preserve_original_revision(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    recorded_at = datetime(2026, 9, 14, 10, 12, tzinfo=UTC)
    observation = _observation()

    with EventLedger(path, clock=lambda: recorded_at) as ledger:
        first = ledger.append(observation)

    later_seen = observation.model_copy(
        update={"first_seen_at": datetime(2026, 9, 14, 10, 20, tzinfo=UTC)}
    )
    with EventLedger(
        path, clock=lambda: datetime(2026, 9, 14, 10, 30, tzinfo=UTC)
    ) as reopened:
        duplicate = reopened.append(later_seen)
        revisions = reopened.revisions()

    assert duplicate == first
    assert revisions == (first,)
    assert first.recorded_at == recorded_at
    assert first.available_at == recorded_at
    assert first.observation.first_seen_at == observation.first_seen_at
    with pytest.raises(ValidationError):
        first.revision = 2


def test_as_of_preserves_the_version_known_at_each_time(tmp_path: Path) -> None:
    clocks = iter(
        (
            datetime(2026, 9, 14, 10, 12, tzinfo=UTC),
            datetime(2026, 9, 14, 14, 5, tzinfo=UTC),
        )
    )
    with EventLedger(tmp_path / "events.sqlite3", clock=lambda: next(clocks)) as ledger:
        morning = ledger.append(_observation())
        afternoon = ledger.append(
            _observation(
                headline="Issuer wins larger contract",
                updated_at=datetime(2026, 9, 14, 14, 0, tzinfo=UTC),
                first_seen_at=datetime(2026, 9, 14, 14, 2, tzinfo=UTC),
            )
        )

        assert ledger.as_of(datetime(2026, 9, 14, 10, 12, tzinfo=UTC)) == (morning,)
        assert ledger.as_of(datetime(2026, 9, 14, 14, 5, tzinfo=UTC)) == (afternoon,)


def test_append_many_rolls_back_when_event_origin_changes(tmp_path: Path) -> None:
    clocks = iter(
        (
            datetime(2026, 9, 14, 10, 0, tzinfo=UTC),
            datetime(2026, 9, 14, 11, 0, tzinfo=UTC),
        )
    )
    original = _observation(origin="historical", first_seen_at=None)
    with EventLedger(tmp_path / "events.sqlite3", clock=lambda: next(clocks)) as ledger:
        stored = ledger.append(original)
        with pytest.raises(ValueError, match="origin cannot change"):
            ledger.append_many(
                (
                    _observation(source_event_id="new-event", origin="historical"),
                    original.model_copy(update={"origin": "forward"}),
                )
            )
        assert ledger.revisions() == (stored,)


def test_exact_reprints_share_a_cluster_without_losing_source_rows(tmp_path: Path) -> None:
    clocks = iter(
        datetime(2026, 9, 14, hour, 0, tzinfo=UTC) for hour in (10, 11, 12)
    )
    with EventLedger(tmp_path / "events.sqlite3", clock=lambda: next(clocks)) as ledger:
        first, copy, changed = ledger.append_many(
            (
                _observation(),
                _observation(source="wire.two", source_event_id="event-2"),
                _observation(
                    source="wire.three",
                    source_event_id="event-3",
                    headline="Issuer wins contract.",
                ),
            )
        )
        revisions = ledger.revisions()

    assert len(revisions) == 3
    assert first.event_id != copy.event_id
    assert first.event_cluster_id == copy.event_cluster_id
    assert changed.event_cluster_id != first.event_cluster_id


def test_canonical_adapter_maps_first_seen_and_submits_only_complete_batches(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.sqlite3"
    recorded_at = datetime(2026, 9, 14, 10, 12, tzinfo=UTC)
    frame = _canonical_frame()
    with EventLedger(path, clock=lambda: recorded_at) as ledger:
        revision = ingest_catalyst_events(frame, ledger=ledger, origin="forward")[0]

    assert revision.recorded_at == recorded_at
    assert revision.observation.first_seen_at == datetime(
        2026, 9, 14, 10, 10, tzinfo=UTC
    )
    assert revision.observation.tags == ("contract",)

    invalid_batch = pl.concat(
        (
            frame,
            frame.with_columns(
                pl.lit(" ").alias("source"),
                pl.lit("news-2").alias("source_event_id"),
            ),
        )
    )
    with EventLedger(
        tmp_path / "invalid.sqlite3", clock=lambda: recorded_at
    ) as empty_ledger:
        with pytest.raises(ValidationError):
            ingest_catalyst_events(invalid_batch, ledger=empty_ledger, origin="forward")
        assert empty_ledger.revisions() == ()
