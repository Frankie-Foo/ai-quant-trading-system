from __future__ import annotations

import polars as pl

from data_plane.http import DownloadError
from scripts.build_catalyst_snapshot import _optional_sec_filings


def test_optional_sec_filings_degrades_to_an_empty_unavailable_frame() -> None:
    def unavailable() -> pl.DataFrame:
        raise DownloadError("SEC unavailable")

    frame, available = _optional_sec_filings(unavailable)

    assert frame.is_empty()
    assert available is False
