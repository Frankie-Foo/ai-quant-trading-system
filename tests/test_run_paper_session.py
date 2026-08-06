from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from scripts.run_paper_session import _current_trade_date


def test_paper_service_resolves_new_york_session_date() -> None:
    assert _current_trade_date(datetime(2026, 7, 21, 13, 25, tzinfo=UTC)) == date(
        2026, 7, 21
    )


def test_paper_service_refuses_weekend() -> None:
    with pytest.raises(RuntimeError, match="not an XNYS"):
        _current_trade_date(datetime(2026, 7, 19, 14, 0, tzinfo=UTC))
