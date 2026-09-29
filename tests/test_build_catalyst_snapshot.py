from __future__ import annotations

import polars as pl

from data_plane.http import DownloadError
from scripts.build_catalyst_snapshot import _optional_sec_filings, _sec_coverage_checks


def test_optional_sec_filings_degrades_to_an_empty_unavailable_frame() -> None:
    def unavailable() -> pl.DataFrame:
        raise DownloadError("SEC unavailable")

    frame, available = _optional_sec_filings(unavailable)

    assert frame.is_empty()
    assert available is False


def test_sec_coverage_checks_preserve_absence_and_scope() -> None:
    coverage, count = _sec_coverage_checks("not_scanned_live", 2569)
    assert coverage.name == "sec_coverage"
    assert coverage.observed == "not_scanned_live"
    assert coverage.passed is False
    assert count.name == "sec_cik_count"
    assert count.observed == "2569"
