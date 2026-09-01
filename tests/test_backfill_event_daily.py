from scripts.backfill_event_daily import _chunks


def test_event_daily_chunks_are_stable_and_complete() -> None:
    assert _chunks(("A", "B", "C", "D", "E"), 2) == [
        ("A", "B"),
        ("C", "D"),
        ("E",),
    ]
