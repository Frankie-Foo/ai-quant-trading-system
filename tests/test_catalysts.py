from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest

from data_plane.calendar import build_xnys_schedule
from data_plane.catalysts import CATALYST_COLUMNS, audit_catalysts, canonicalize_catalysts
from data_plane.providers.sec_filings import _parse_master_index, _recent_by_accession
from kernel.catalysts import (
    build_catalyst_candidates,
    parse_earnings_headline,
    prepare_catalysts,
    select_overnight_catalysts,
)


def _events() -> pl.DataFrame:
    long_summary = (
        "The company reported a material development with enough concrete detail for "
        "a decision-time evidence record and independent verification by the research system."
    )
    rows = [
        {
            "source": "alpaca.news.benzinga",
            "source_event_id": "101",
            "event_type": "news",
            "event_subtype": None,
            "published_utc": datetime(2026, 7, 17, 21, 0, tzinfo=UTC),
            "updated_utc": datetime(2026, 7, 17, 21, 1, tzinfo=UTC),
            "retrieved_utc": datetime(2026, 7, 19, 23, 0, tzinfo=UTC),
            "symbols": ["FAST"],
            "headline": "Fast Corp signs a material multi-year customer contract",
            "summary": long_summary,
            "publisher": "Benzinga",
            "url": "https://example.test/fast-contract",
            "cik": None,
            "accession_number": None,
            "form_items": [],
            "tags": ["contracts"],
            "provenance": "alpaca.news:101",
        },
        {
            "source": "massive.news",
            "source_event_id": "m-101",
            "event_type": "news",
            "event_subtype": None,
            "published_utc": datetime(2026, 7, 17, 21, 2, tzinfo=UTC),
            "updated_utc": None,
            "retrieved_utc": datetime(2026, 7, 19, 23, 0, tzinfo=UTC),
            "symbols": ["FAST"],
            "headline": "Fast Corp signs a material multi-year customer contract",
            "summary": long_summary,
            "publisher": "Newswire",
            "url": "https://example.test/fast-contract-copy",
            "cik": None,
            "accession_number": None,
            "form_items": [],
            "tags": ["contracts"],
            "provenance": "massive.news:m-101",
        },
        {
            "source": "massive.news",
            "source_event_id": "macro",
            "event_type": "news",
            "event_subtype": None,
            "published_utc": datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
            "updated_utc": None,
            "retrieved_utc": datetime(2026, 7, 19, 23, 0, tzinfo=UTC),
            "symbols": ["A", "B", "C", "D"],
            "headline": "Four stocks investors are watching this weekend",
            "summary": long_summary,
            "publisher": "Publisher",
            "url": "https://example.test/macro",
            "cik": None,
            "accession_number": None,
            "form_items": [],
            "tags": [],
            "provenance": "massive.news:macro",
        },
        {
            "source": "alpaca.news.benzinga",
            "source_event_id": "short",
            "event_type": "news",
            "event_subtype": None,
            "published_utc": datetime(2026, 7, 19, 15, 0, tzinfo=UTC),
            "updated_utc": None,
            "retrieved_utc": datetime(2026, 7, 19, 23, 0, tzinfo=UTC),
            "symbols": ["FAST"],
            "headline": "Fast Corp update",
            "summary": "Shares moved.",
            "publisher": "Benzinga",
            "url": "https://example.test/short",
            "cik": None,
            "accession_number": None,
            "form_items": [],
            "tags": [],
            "provenance": "alpaca.news:short",
        },
        {
            "source": "sec.submissions",
            "source_event_id": "0001-26-000001",
            "event_type": "sec_filing",
            "event_subtype": "8-K",
            "published_utc": datetime(2026, 7, 17, 22, 0, tzinfo=UTC),
            "updated_utc": None,
            "retrieved_utc": datetime(2026, 7, 19, 23, 0, tzinfo=UTC),
            "symbols": ["EARN"],
            "headline": "EARN 8-K filing",
            "summary": "Item 2.02 Results of Operations and Financial Condition",
            "publisher": "SEC EDGAR",
            "url": "https://www.sec.gov/Archives/example",
            "cik": "0000000001",
            "accession_number": "0001-26-000001",
            "form_items": ["2.02"],
            "tags": ["8-K"],
            "provenance": "sec.submissions:0001-26-000001",
        },
        {
            "source": "massive.news",
            "source_event_id": "future",
            "event_type": "news",
            "event_subtype": None,
            "published_utc": datetime(2026, 7, 20, 1, 0, tzinfo=UTC),
            "updated_utc": None,
            "retrieved_utc": datetime(2026, 7, 20, 1, 1, tzinfo=UTC),
            "symbols": ["FAST"],
            "headline": "Fast Corp receives regulatory approval for a new treatment",
            "summary": long_summary,
            "publisher": "Publisher",
            "url": "https://example.test/future",
            "cik": None,
            "accession_number": None,
            "form_items": [],
            "tags": ["FDA"],
            "provenance": "massive.news:future",
        },
    ]
    return canonicalize_catalysts(pl.DataFrame(rows))


def test_canonical_catalyst_contract_and_quality() -> None:
    events = _events()
    assert tuple(events.columns) == CATALYST_COLUMNS
    assert str(events.schema["published_utc"]) == "Datetime(time_unit='ms', time_zone='UTC')"
    checks = audit_catalysts(
        events,
        provenance="test.events",
        start_utc=datetime(2026, 7, 17, 20, 0, tzinfo=UTC),
        end_utc=datetime(2026, 7, 20, 2, 0, tzinfo=UTC),
    )
    assert all(check.passed for check in checks if check.severity.value == "critical")


def test_canonical_catalysts_parse_rfc3339_provider_timestamps() -> None:
    raw = _events().head(1).with_columns(
        pl.col("published_utc").dt.to_string("%Y-%m-%dT%H:%M:%SZ"),
        pl.col("updated_utc").dt.to_string("%Y-%m-%dT%H:%M:%SZ"),
        pl.col("retrieved_utc").dt.to_string("%Y-%m-%dT%H:%M:%SZ"),
    )
    normalized = canonicalize_catalysts(raw)
    assert normalized.get_column("published_utc")[0] == datetime(
        2026, 7, 17, 21, 0, tzinfo=UTC
    )


def test_preparation_enforces_cleaning_dedup_and_no_future() -> None:
    prepared = prepare_catalysts(
        _events(), asof_utc=datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    )
    reasons = dict(
        prepared.select("source_event_id", "exclude_reason").iter_rows()
    )
    assert reasons["m-101"] == "duplicate_event_chain"
    assert reasons["macro"] == "broad_multi_company_review"
    assert reasons["short"] == "insufficient_text"
    assert reasons["future"] == "published_after_asof"

    contract = prepared.filter(pl.col("source_event_id") == "101").row(0, named=True)
    earnings = prepared.filter(
        pl.col("source_event_id") == "0001-26-000001"
    ).row(0, named=True)
    assert contract["eligible"] is True
    assert contract["catalyst_category"] == "contract_partnership"
    assert set(contract["corroborating_sources"]) == {
        "alpaca.news.benzinga",
        "massive.news",
    }
    assert contract["source_count"] == 2
    assert earnings["eligible"] is True
    assert earnings["catalyst_category"] == "earnings"


def test_structured_earnings_headlines_preserve_short_material_evidence() -> None:
    actual = parse_earnings_headline(
        "Example Q2 Adj. EPS $1.22 Beats $1.16 Estimate, "
        "Sales $657.011M Beat $650.530M Estimate"
    )
    quarterly = parse_earnings_headline(
        "Example Sees Q3 Adj EPS $1.25-$1.30 vs $1.25 Est; "
        "Sees Sales $664.000M-$670.000M vs $663.204M Est"
    )
    annual = parse_earnings_headline(
        "Example Raises FY2026 Adj EPS Guidance from $4.85-$5.01 "
        "to $4.96-$5.10 vs $4.92 Est; Raises FY2026 Sales Guidance "
        "from $2.620B-$2.640B to $2.635B-$2.646B vs $2.631B Est"
    )

    assert actual.structured is True
    assert actual.actual_eps_surprise == pytest.approx(1.22 / 1.16 - 1)
    assert actual.actual_revenue_surprise == pytest.approx(657.011 / 650.530 - 1)
    assert quarterly.forward_eps_vs_consensus == pytest.approx(1.275 / 1.25 - 1)
    assert quarterly.forward_revenue_vs_consensus == pytest.approx(
        667.0 / 663.204 - 1
    )
    assert annual.eps_guidance_raise == pytest.approx(5.03 / 4.93 - 1)
    assert annual.revenue_guidance_raise == pytest.approx(2.6405 / 2.63 - 1)


def test_earnings_intensity_aggregates_actual_guide_and_raise_layers() -> None:
    base = _events().head(1)
    headlines = [
        (
            "earnings-actual",
            "Example Q2 Adj. EPS $1.22 Beats $1.16 Estimate, "
            "Sales $657.011M Beat $650.530M Estimate",
        ),
        (
            "earnings-quarterly",
            "Example Sees Q3 Adj EPS $1.25-$1.30 vs $1.25 Est; "
            "Sees Sales $664.000M-$670.000M vs $663.204M Est",
        ),
        (
            "earnings-annual",
            "Example Raises FY2026 Adj EPS Guidance from $4.85-$5.01 "
            "to $4.96-$5.10 vs $4.92 Est; Raises FY2026 Sales Guidance "
            "from $2.620B-$2.640B to $2.635B-$2.646B vs $2.631B Est",
        ),
    ]
    frames = [
        base.with_columns(
            pl.lit(event_id).alias("source_event_id"),
            pl.lit(["EARN"]).alias("symbols"),
            pl.lit(headline).alias("headline"),
            pl.lit("").alias("summary"),
        )
        for event_id, headline in headlines
    ]
    prepared = prepare_catalysts(
        canonicalize_catalysts(pl.concat(frames)),
        asof_utc=datetime(2026, 7, 20, 0, 0, tzinfo=UTC),
    )
    assert prepared.get_column("eligible").to_list() == [True, True, True]
    assert prepared.get_column("catalyst_category").to_list() == [
        "earnings",
        "earnings",
        "earnings",
    ]
    overnight = prepared.with_columns(
        pl.lit(date(2026, 7, 20)).cast(pl.Date).alias("session_date")
    )
    candidate = build_catalyst_candidates(
        pl.DataFrame({"symbol": ["EARN"], "precheck_pass": [True]}),
        overnight,
    ).row(0, named=True)

    assert candidate["event_count"] == 3
    assert candidate["earnings_evidence_layers"] == 3
    assert candidate["earnings_strength_confirmed"] is True
    assert candidate["earnings_intensity_score"] >= 70


def test_weekend_news_maps_to_next_session_without_post_asof_events() -> None:
    schedule = build_xnys_schedule(date(2026, 7, 17), date(2026, 7, 20))
    prepared = prepare_catalysts(
        _events(), asof_utc=datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    )
    selected = select_overnight_catalysts(
        prepared,
        schedule=schedule,
        target_date=date(2026, 7, 20),
        asof_utc=datetime(2026, 7, 20, 0, 0, tzinfo=UTC),
    )
    assert set(selected.get_column("source_event_id")) == {
        "101",
        "0001-26-000001",
    }
    assert selected.get_column("session_date").unique().to_list() == [date(2026, 7, 20)]


def test_catalyst_candidates_only_include_daily_precheck_symbols() -> None:
    schedule = build_xnys_schedule(date(2026, 7, 17), date(2026, 7, 20))
    prepared = prepare_catalysts(
        _events(), asof_utc=datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    )
    selected = select_overnight_catalysts(
        prepared,
        schedule=schedule,
        target_date=date(2026, 7, 20),
        asof_utc=datetime(2026, 7, 20, 0, 0, tzinfo=UTC),
    )
    universe = pl.DataFrame(
        {"symbol": ["FAST", "EARN"], "precheck_pass": [True, False]}
    )
    candidates = build_catalyst_candidates(universe, selected)
    assert candidates.get_column("symbol").to_list() == ["FAST"]
    row = candidates.row(0, named=True)
    assert row["event_count"] == 1
    assert row["independent_source_count"] == 2
    assert row["catalyst_categories"] == ["contract_partnership"]


def test_sec_index_and_columnar_submissions_preserve_accession_identity() -> None:
    index = _parse_master_index(
        "\n".join(
            [
                "Description: Daily Index",
                "CIK|Company Name|Form Type|Date Filed|File Name",
                "-----",
                "1|Example Corp|8-K|20260717|edgar/data/1/0000000001-26-000001.txt",
            ]
        )
    )
    recent = _recent_by_accession(
        {
            "accessionNumber": ["0000000001-26-000001"],
            "acceptanceDateTime": ["2026-07-17T21:01:02.000Z"],
            "form": ["8-K"],
            "items": ["2.02"],
        }
    )
    assert index[0]["form"] == "8-K"
    assert index[0]["cik"] == "1"
    assert recent["0000000001-26-000001"]["items"] == "2.02"


def test_real_data_noise_families_are_not_treated_as_catalysts() -> None:
    base = _events().head(1)
    editorial = base.with_columns(
        pl.lit("editorial").alias("source_event_id"),
        pl.lit("The Motley Fool").alias("publisher"),
        pl.lit("A thoughtful long-form opinion about Fast Corp").alias("headline"),
    )
    legal = base.with_columns(
        pl.lit("legal").alias("source_event_id"),
        pl.lit("Shareholder alert: lead plaintiff deadline in class action").alias(
            "headline"
        ),
    )
    evergreen = base.with_columns(
        pl.lit("evergreen").alias("source_event_id"),
        pl.lit("Here's how much $1000 invested in Fast Corp would have earned").alias(
            "headline"
        ),
    )
    prepared = prepare_catalysts(
        canonicalize_catalysts(pl.concat([editorial, legal, evergreen])),
        asof_utc=datetime(2026, 7, 20, 0, 0, tzinfo=UTC),
    )
    reasons = dict(prepared.select("source_event_id", "exclude_reason").iter_rows())
    assert reasons == {
        "editorial": "editorial_analysis",
        "legal": "legal_solicitation",
        "evergreen": "evergreen_backward_looking",
    }
