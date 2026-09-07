"""Execution-price boundaries: synthetic NBBO, no broker calls."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from execution.alpaca_paper import FreshNbboQuote, build_protected_entry


def test_cent_rounding_cannot_exceed_approved_signal_slippage() -> None:
    now = datetime(2026, 9, 8, 14, tzinfo=UTC)
    quote = FreshNbboQuote(
        symbol="TEST", bid=Decimal("50.12"), ask=Decimal("50.124"),
        asof_utc=now, feed="sip",
    )
    with pytest.raises(ValueError, match="rounded.*slippage"):
        build_protected_entry(
            client_order_id="rounding-test", symbol="TEST", qty=10,
            signal_reference=Decimal("50"), structural_stop=Decimal("49.90"),
            quote=quote, observed_at_utc=now,
        )


def test_rounded_stop_and_entry_still_obey_all_in_stop_ceiling() -> None:
    now = datetime(2026, 9, 8, 14, tzinfo=UTC)
    quote = FreshNbboQuote(
        symbol="TEST", bid=Decimal("10"), ask=Decimal("10.001"),
        asof_utc=now, feed="sip",
    )
    with pytest.raises(ValueError, match="rounded.*stop"):
        build_protected_entry(
            client_order_id="stop-rounding-test", symbol="TEST", qty=10,
            signal_reference=Decimal("10.01"), structural_stop=Decimal("9.851"),
            quote=quote, observed_at_utc=now,
            stop_slippage_reserve=Decimal("0.005"),
        )
