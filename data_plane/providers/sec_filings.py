from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime

import polars as pl

from data_plane.catalysts import canonicalize_catalysts, empty_catalyst_frame
from data_plane.http import DownloadError, get_json, get_response

RELEVANT_FORMS = frozenset(
    {
        "8-K",
        "8-K/A",
        "6-K",
        "6-K/A",
        "10-Q",
        "10-Q/A",
        "10-K",
        "10-K/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
        "S-1",
        "S-1/A",
        "S-3",
        "S-3/A",
        "F-1",
        "F-1/A",
        "F-3",
        "F-3/A",
        "424B3",
        "424B4",
        "424B5",
        "SC 13D",
        "SC 13D/A",
        "SC TO-I",
        "SC TO-T",
        "DEFM14A",
        "PREM14A",
    }
)


def sec_user_agent() -> str:
    value = os.getenv("SEC_USER_AGENT", "").strip()
    if not value:
        raise RuntimeError("missing SEC_USER_AGENT with a descriptive contact address")
    return value


def fetch_candidate_filings(
    filing_dates: Iterable[date],
    *,
    cik_to_symbols: Mapping[str, tuple[str, ...]],
    start_utc: datetime,
    end_utc: datetime,
    pace_seconds: float = 0.2,
) -> pl.DataFrame:
    if start_utc.tzinfo is None or end_utc.tzinfo is None:
        raise ValueError("SEC bounds must be timezone-aware")
    if end_utc <= start_utc:
        raise ValueError("SEC end must be after start")
    if pace_seconds < 0:
        raise ValueError("pace_seconds must be nonnegative")

    normalized_map = {
        _normalize_cik(cik): tuple(sorted(set(symbols)))
        for cik, symbols in cik_to_symbols.items()
        if symbols
    }
    index_rows: list[dict[str, str]] = []
    for filing_date in sorted(set(filing_dates)):
        if filing_date.weekday() >= 5:
            continue
        text = get_response(
            _daily_index_url(filing_date), headers=_sec_headers()
        ).text
        index_rows.extend(_parse_master_index(text))

    matched = [
        row
        for row in index_rows
        if _normalize_cik(row["cik"]) in normalized_map and row["form"] in RELEVANT_FORMS
    ]
    if not matched:
        return empty_catalyst_frame()

    by_cik: dict[str, dict[str, dict[str, object]]] = {}
    ciks = sorted({_normalize_cik(row["cik"]) for row in matched})

    def fetch_recent(cik: str) -> tuple[str, dict[str, dict[str, object]]]:
        payload = get_json(
            f"https://data.sec.gov/submissions/CIK{cik}.json", headers=_sec_headers()
        )
        recent = payload.get("filings", {}).get("recent", {})
        if not isinstance(recent, dict):
            raise DownloadError(f"SEC submissions response for CIK{cik} has no recent filings")
        return cik, _recent_by_accession(recent)

    # Keep the same four-request ceiling as the live path while avoiding a
    # multi-minute serial scan for a broad candidate universe.
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_recent, cik): cik for cik in ciks}
        for future in as_completed(futures):
            cik, recent = future.result()
            by_cik[cik] = recent

    retrieved = datetime.now(UTC)
    rows: list[dict[str, object]] = []
    for indexed in matched:
        cik = _normalize_cik(indexed["cik"])
        accession = _accession_from_filename(indexed["filename"])
        filing = by_cik[cik].get(accession)
        if filing is None:
            raise DownloadError(
                f"SEC filing {accession} is absent from current submissions for CIK{cik}"
            )
        accepted = filing.get("acceptanceDateTime")
        if not accepted:
            raise DownloadError(f"SEC filing {accession} has no acceptanceDateTime")
        accepted_at = _parse_sec_timestamp(str(accepted))
        if accepted_at < start_utc or accepted_at >= end_utc:
            continue
        primary_document = str(filing.get("primaryDocument") or "")
        description = str(filing.get("primaryDocDescription") or "").strip()
        form = str(filing.get("form") or indexed["form"])
        items = _split_items(filing.get("items"))
        symbols = list(normalized_map[cik])
        rows.append(
            {
                "source": "sec.submissions",
                "source_event_id": accession,
                "event_type": "sec_filing",
                "event_subtype": form,
                "published_utc": accepted_at,
                "updated_utc": None,
                "retrieved_utc": retrieved,
                "symbols": symbols,
                "headline": f"{','.join(symbols)} {form} filing: {description}".strip(),
                "summary": _filing_summary(recent, items),
                "publisher": "SEC EDGAR",
                "url": _filing_url(cik, accession, primary_document),
                "cik": cik,
                "accession_number": accession,
                "form_items": items,
                "tags": [form],
                "provenance": f"sec.submissions:{accession}",
            }
        )
    return canonicalize_catalysts(pl.DataFrame(rows) if rows else empty_catalyst_frame())


def fetch_live_candidate_filings(
    *,
    cik_to_symbols: Mapping[str, tuple[str, ...]],
    start_utc: datetime,
    end_utc: datetime,
    max_workers: int = 4,
) -> pl.DataFrame:
    """Check current submissions directly for a previously locked symbol set."""
    if start_utc.tzinfo is None or end_utc.tzinfo is None:
        raise ValueError("SEC bounds must be timezone-aware")
    if end_utc <= start_utc:
        raise ValueError("SEC end must be after start")
    if not 1 <= max_workers <= 4:
        raise ValueError("max_workers must be between 1 and 4 for SEC fair access")
    normalized_map = {
        _normalize_cik(cik): tuple(sorted(set(symbols)))
        for cik, symbols in cik_to_symbols.items()
        if symbols
    }
    retrieved = datetime.now(UTC)
    rows: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                get_json,
                f"https://data.sec.gov/submissions/CIK{cik}.json",
                headers=_sec_headers(),
            ): cik
            for cik in normalized_map
        }
        for future in as_completed(futures):
            cik = futures[future]
            payload = future.result()
            recent = payload.get("filings", {}).get("recent", {})
            if not isinstance(recent, dict):
                raise DownloadError(
                    f"SEC submissions response for CIK{cik} has no recent filings"
                )
            for accession, filing in _recent_by_accession(recent).items():
                form = str(filing.get("form") or "")
                accepted = filing.get("acceptanceDateTime")
                if form not in RELEVANT_FORMS or not accepted:
                    continue
                accepted_at = _parse_sec_timestamp(str(accepted))
                if accepted_at < start_utc or accepted_at >= end_utc:
                    continue
                rows.append(
                    _filing_event_row(
                        cik=cik,
                        symbols=normalized_map[cik],
                        accession=accession,
                        recent=filing,
                        retrieved=retrieved,
                    )
                )
    return canonicalize_catalysts(pl.DataFrame(rows) if rows else empty_catalyst_frame())


def _sec_headers() -> dict[str, str]:
    return {"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"}


def _daily_index_url(filing_date: date) -> str:
    quarter = (filing_date.month - 1) // 3 + 1
    return (
        f"https://www.sec.gov/Archives/edgar/daily-index/{filing_date.year}/"
        f"QTR{quarter}/master.{filing_date:%Y%m%d}.idx"
    )


def _parse_master_index(text: str) -> list[dict[str, str]]:
    header = "CIK|Company Name|Form Type|Date Filed|File Name"
    lines = text.splitlines()
    try:
        start = lines.index(header) + 2
    except ValueError as exc:
        raise DownloadError("SEC master index header was not found") from exc
    rows: list[dict[str, str]] = []
    for line in lines[start:]:
        if not line.strip():
            continue
        parts = line.split("|", 4)
        if len(parts) != 5:
            raise DownloadError("SEC master index contained a malformed row")
        rows.append(
            {
                "cik": parts[0],
                "company_name": parts[1],
                "form": parts[2],
                "filing_date": parts[3],
                "filename": parts[4],
            }
        )
    return rows


def _recent_by_accession(recent: Mapping[str, object]) -> dict[str, dict[str, object]]:
    accessions = recent.get("accessionNumber", [])
    if not isinstance(accessions, list):
        raise DownloadError("SEC recent filings accessionNumber is not a list")
    columns = {key: value for key, value in recent.items() if isinstance(value, list)}
    output: dict[str, dict[str, object]] = {}
    for index, accession in enumerate(accessions):
        output[str(accession)] = {
            key: values[index] if index < len(values) else None
            for key, values in columns.items()
        }
    return output


def _normalize_cik(value: str) -> str:
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        raise ValueError(f"invalid CIK: {value!r}")
    return digits.zfill(10)


def _accession_from_filename(filename: str) -> str:
    match = re.search(r"(\d{10}-\d{2}-\d{6})", filename)
    if not match:
        raise DownloadError(f"SEC filename has no accession number: {filename}")
    return match.group(1)


def _parse_sec_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _split_items(value: object) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _filing_summary(recent: Mapping[str, object], items: list[str]) -> str:
    parts = []
    report_date = str(recent.get("reportDate") or "").strip()
    description = str(recent.get("primaryDocDescription") or "").strip()
    if description:
        parts.append(description)
    if items:
        parts.append(f"Items: {', '.join(items)}")
    if report_date:
        parts.append(f"Report date: {report_date}")
    return ". ".join(parts)


def _filing_event_row(
    *,
    cik: str,
    symbols: tuple[str, ...],
    accession: str,
    recent: Mapping[str, object],
    retrieved: datetime,
) -> dict[str, object]:
    accepted = recent.get("acceptanceDateTime")
    if not accepted:
        raise DownloadError(f"SEC filing {accession} has no acceptanceDateTime")
    accepted_at = _parse_sec_timestamp(str(accepted))
    primary_document = str(recent.get("primaryDocument") or "")
    description = str(recent.get("primaryDocDescription") or "").strip()
    form = str(recent.get("form") or "")
    items = _split_items(recent.get("items"))
    symbol_list = list(symbols)
    return {
        "source": "sec.submissions",
        "source_event_id": accession,
        "event_type": "sec_filing",
        "event_subtype": form,
        "published_utc": accepted_at,
        "updated_utc": None,
        "retrieved_utc": retrieved,
        "symbols": symbol_list,
        "headline": f"{','.join(symbol_list)} {form} filing: {description}".strip(),
        "summary": _filing_summary(recent, items),
        "publisher": "SEC EDGAR",
        "url": _filing_url(cik, accession, primary_document),
        "cik": cik,
        "accession_number": accession,
        "form_items": items,
        "tags": [form],
        "provenance": f"sec.submissions:{accession}",
    }


def _filing_url(cik: str, accession: str, primary_document: str) -> str:
    accession_compact = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{accession_compact}/{primary_document}"
    )
