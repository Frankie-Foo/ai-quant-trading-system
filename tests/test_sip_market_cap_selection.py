from datetime import UTC, date, datetime

import polars as pl

from scripts.build_selection_gates import _market_details_from_sip_frames


def test_selection_uses_only_fresh_pit_sip_market_caps() -> None:
    decision_at = datetime(2026, 9, 14, 13, 0, tzinfo=UTC)
    details = _market_details_from_sip_frames(
        [
            pl.DataFrame(
                {
                    "symbol": ["GOOD", "STALE", "FUTURE", "WRONG"],
                    "market_cap": [2e9, 3e9, 4e9, 5e9],
                    "asof_date": [date(2026, 9, 14)] * 4,
                    "available_at": [
                        datetime(2026, 9, 14, 12, 59, 59, tzinfo=UTC),
                        datetime(2026, 9, 14, 12, 59, 30, tzinfo=UTC),
                        datetime(2026, 9, 14, 13, 0, 1, tzinfo=UTC),
                        datetime(2026, 9, 14, 12, 59, 59, tzinfo=UTC),
                    ],
                    "provenance": ["good", "stale", "future", "wrong-feed"],
                    "market_cap_status": ["available"] * 4,
                    "source": ["derived.sip_market_cap"] * 4,
                    "price_source": [
                        "alpaca.sip.rest.trades",
                        "alpaca.sip.rest.trades",
                        "alpaca.sip.rest.trades",
                        "manual.test",
                    ],
                    "price_timestamp": [
                        datetime(2026, 9, 14, 12, 59, 50, tzinfo=UTC),
                        datetime(2026, 9, 14, 12, 49, 59, tzinfo=UTC),
                        datetime(2026, 9, 14, 12, 59, 59, tzinfo=UTC),
                        datetime(2026, 9, 14, 12, 59, 59, tzinfo=UTC),
                    ],
                }
            )
        ],
        symbols=("GOOD", "STALE", "FUTURE", "WRONG"),
        decision_at=decision_at,
    )

    assert details.to_dicts() == [
        {
            "symbol": "GOOD",
            "market_cap": 2_000_000_000.0,
            "asof_date": date(2026, 9, 14),
            "provenance": "good",
        }
    ]
