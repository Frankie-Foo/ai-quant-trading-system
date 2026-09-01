from datetime import UTC, date, datetime, timedelta

import polars as pl

from data_plane.calendar import build_xnys_schedule
from research.event_rvol import build_event_rvol_cohort


def test_event_rvol_uses_only_twenty_prior_sessions() -> None:
    schedule = build_xnys_schedule(date(2026, 6, 1), date(2026, 7, 1))
    sessions = schedule.get_column("trade_date").to_list()
    target = sessions[-1]
    rows = [
        {
            "symbol": "TEST",
            "ts_utc": datetime.combine(session, datetime.min.time(), UTC)
            + timedelta(hours=10),
            "volume": 100,
        }
        for session in sessions[-21:-1]
    ]
    gap = pl.DataFrame(
        {
            "session_date": [target],
            "symbol": ["TEST"],
            "premarket_volume": [200],
            "catalyst_tier": [2],
            "premarket_gap_return": [0.05],
        }
    )

    result = build_event_rvol_cohort(gap, pl.DataFrame(rows), schedule=schedule)

    assert result.row(0, named=True)["premarket_rvol"] == 2.0
    assert result.row(0, named=True)["selection_rank"] == 1
