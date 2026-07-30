from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from perp_risk.liquidations import (
    JsonlLiquidationSource,
    LiquidationEvent,
    apply_liquidations,
)
from perp_risk.models import PerpObservation

NOW = datetime(2026, 7, 30, 14, tzinfo=UTC)


def _observation() -> PerpObservation:
    return PerpObservation(
        venue="hyperliquid",
        market="main",
        instrument="BTC",
        observed_at_utc=NOW,
        mark_price=100,
        bid_price=99,
        ask_price=101,
        provenance="public",
    )


def test_jsonl_liquidations_are_deduplicated_and_aggregated(
    tmp_path: Path,
) -> None:
    events = [
        LiquidationEvent(
            event_id="long-1",
            venue="hyperliquid",
            market="main",
            instrument="BTC",
            liquidated_side="long",
            notional_usd=100,
            observed_at_utc=NOW,
            provenance="feed:long-1",
        ),
        LiquidationEvent(
            event_id="short-1",
            venue="hyperliquid",
            market="main",
            instrument="BTC",
            liquidated_side="short",
            notional_usd=300,
            observed_at_utc=NOW,
            provenance="feed:short-1",
        ),
    ]
    path = tmp_path / "liquidations.jsonl"
    path.write_text(
        "\n".join(item.model_dump_json() for item in events) + "\n",
        encoding="utf-8",
    )

    fetched = JsonlLiquidationSource(path).fetch(
        start_utc=NOW - timedelta(seconds=60),
        end_utc=NOW,
    )
    enriched = apply_liquidations(
        (_observation(),),
        fetched,
        source_complete=True,
    )[0]

    assert enriched.long_liquidation_usd == 100
    assert enriched.short_liquidation_usd == 300
    assert enriched.liquidation_event_count == 2


def test_complete_empty_window_is_zero_but_unconfigured_stays_unknown() -> None:
    complete = apply_liquidations(
        (_observation(),),
        (),
        source_complete=True,
    )[0]
    unavailable = apply_liquidations((_observation(),), ())[0]

    assert complete.long_liquidation_usd == 0
    assert complete.short_liquidation_usd == 0
    assert complete.liquidation_event_count == 0
    assert unavailable.long_liquidation_usd is None
    assert unavailable.short_liquidation_usd is None


def test_liquidation_event_json_contract_is_portable() -> None:
    event = LiquidationEvent(
        event_id="event",
        venue="hyperliquid",
        market="main",
        instrument="btc",
        liquidated_side="long",
        notional_usd=1,
        observed_at_utc=NOW,
        provenance="test",
    )

    payload = json.loads(event.model_dump_json())
    assert payload["schema_version"] == "perp_risk_liquidation_event.v1"
    assert payload["instrument"] == "BTC"
