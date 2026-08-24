from datetime import date

from scripts.backfill_historical_rth import DEFAULT_PROXIES, plan_sessions


def test_plan_sessions_adds_proxies_and_limits_dates() -> None:
    candidates = {
        date(2026, 8, 14): ("SNDK", "WDC"),
        date(2026, 8, 17): ("REZI",),
    }

    result = plan_sessions(
        candidates,
        start=date(2026, 8, 17),
        end=date(2026, 8, 17),
    )

    assert tuple(result) == (date(2026, 8, 17),)
    assert set(result[date(2026, 8, 17)]) == {"REZI", *DEFAULT_PROXIES}
