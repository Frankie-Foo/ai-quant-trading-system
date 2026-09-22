from datetime import UTC, datetime, timedelta

import polars as pl

from scripts.cache_sec_shares_outstanding import (
    _pending_ciks,
    build_cache_rows,
    build_massive_cache_rows,
    merge_share_cache,
    reference_targets,
)


def test_reference_targets_only_active_common_stocks_with_a_cik() -> None:
    targets = reference_targets(
        pl.DataFrame(
            {
                "symbol": ["B", "A", "ETF", "OLD", "NO_CIK"],
                "cik": ["2", "1", "3", "4", None],
                "security_type": ["CS", "CS", "ETF", "CS", "CS"],
                "active": [True, True, True, False, True],
            }
        )
    )

    assert targets.to_dicts() == [
        {"symbol": "A", "cik": "0000000001"},
        {"symbol": "B", "cik": "0000000002"},
    ]


def test_build_cache_rows_applies_one_sec_share_fact_to_every_share_class() -> None:
    retrieved_at = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    rows = build_cache_rows(
        pl.DataFrame(
            {
                "symbol": ["GOOG", "GOOGL"],
                "cik": ["0001652044", "0001652044"],
            }
        ),
        {
            "0001652044": {
                "facts": {
                    "dei": {
                        "EntityCommonStockSharesOutstanding": {
                            "units": {
                                "shares": [
                                    {
                                        "end": "2026-06-30",
                                        "filed": "2026-07-30",
                                        "form": "10-Q",
                                        "val": 12_000_000_000,
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        },
        retrieved_at=retrieved_at,
    )

    assert rows.select("symbol", "shares_outstanding", "available_at", "cik").to_dicts() == [
        {
            "symbol": "GOOG",
            "shares_outstanding": 12_000_000_000.0,
            "available_at": retrieved_at,
            "cik": "0001652044",
        },
        {
            "symbol": "GOOGL",
            "shares_outstanding": 12_000_000_000.0,
            "available_at": retrieved_at,
            "cik": "0001652044",
        },
    ]
    assert rows.get_column("provenance").to_list() == [
        "sec.companyfacts:CIK0001652044:EntityCommonStockSharesOutstanding@filed=2026-07-30",
        "sec.companyfacts:CIK0001652044:EntityCommonStockSharesOutstanding@filed=2026-07-30",
    ]


def test_merge_share_cache_replaces_only_refreshed_ciks() -> None:
    existing = pl.DataFrame(
        {
            "symbol": ["A", "KEEP"],
            "cik": ["0000000001", "0000000009"],
            "shares_outstanding": [100.0, 900.0],
            "available_at": [
                datetime(2026, 9, 1, tzinfo=UTC),
                datetime(2026, 9, 1, tzinfo=UTC),
            ],
            "source": ["sec.companyfacts"] * 2,
            "provenance": ["old-a", "old-keep"],
        }
    )
    refreshed = pl.DataFrame(
        {
            "symbol": ["A", "A.B"],
            "cik": ["0000000001", "0000000001"],
            "shares_outstanding": [200.0, 200.0],
            "available_at": [
                datetime(2026, 9, 14, tzinfo=UTC),
                datetime(2026, 9, 14, tzinfo=UTC),
            ],
            "source": ["sec.companyfacts"] * 2,
            "provenance": ["new-a", "new-a-b"],
        }
    )

    merged = merge_share_cache(existing, refreshed, refreshed_ciks={"0000000001"})

    assert merged.select("symbol", "shares_outstanding", "provenance").to_dicts() == [
        {"symbol": "A", "shares_outstanding": 200.0, "provenance": "new-a"},
        {"symbol": "A.B", "shares_outstanding": 200.0, "provenance": "new-a-b"},
        {"symbol": "KEEP", "shares_outstanding": 900.0, "provenance": "old-keep"},
    ]


def test_build_massive_cache_rows_uses_weighted_shares_only() -> None:
    retrieved_at = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    rows = build_massive_cache_rows(
        pl.DataFrame({"symbol": ["RDDT"], "cik": ["0001713445"]}),
        pl.DataFrame(
            {
                "symbol": ["RDDT"],
                "weighted_shares_outstanding": [192_396_510.0],
                "share_class_shares_outstanding": [146_103_200.0],
                "provenance": ["massive.ticker_details:RDDT@2026-09-14"],
            }
        ),
        retrieved_at=retrieved_at,
    )

    assert rows.select("symbol", "shares_outstanding", "source", "fact_tag").to_dicts() == [
        {
            "symbol": "RDDT",
            "shares_outstanding": 192_396_510.0,
            "source": "massive.ticker_details.weighted_shares_outstanding",
            "fact_tag": "weighted_shares_outstanding",
        }
    ]


def test_pending_ciks_skips_fresh_complete_cache_and_honors_backoff() -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    targets = pl.DataFrame(
        {
            "symbol": ["READY", "RETRY"],
            "cik": ["0000000001", "0000000002"],
        }
    )
    cache = pl.DataFrame(
        {
            "symbol": ["READY"],
            "cik": ["0000000001"],
        }
    )
    state = {
        "0000000001": {"status": "available", "updated_at": now, "retry_after": None},
        "0000000002": {
            "status": "download_error",
            "updated_at": now,
            "retry_after": datetime(2026, 9, 14, 13, 0, tzinfo=UTC),
        },
    }

    assert _pending_ciks(
        targets,
        cache,
        state,
        now=now,
        refresh_after=timedelta(days=14),
    ) == ()
