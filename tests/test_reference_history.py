from __future__ import annotations

from datetime import date

from scripts.backfill_massive_reference_weekly import reference_plan


def test_reference_plan_covers_each_target_with_strictly_prior_weekly_snapshot() -> None:
    targets, anchors = reference_plan(end_date=date(2026, 7, 16), sessions=252)

    assert len(targets) == 252
    assert targets[-1] == date(2026, 7, 16)
    assert len(anchors) >= 50
    assert len(anchors) <= 55
    assert all(any(anchor < target for anchor in anchors) for target in targets)

    # A fixed weekly plan must not create a special same-week snapshot merely because
    # the requested history ends in the middle of a week.
    assert date(2026, 7, 16) not in anchors
    assert date(2026, 7, 17) not in anchors


def test_reference_plan_rejects_invalid_session_count() -> None:
    try:
        reference_plan(end_date=date(2026, 7, 16), sessions=0)
    except ValueError as exc:
        assert "sessions" in str(exc)
    else:
        raise AssertionError("zero sessions must fail")
