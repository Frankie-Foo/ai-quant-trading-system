from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from perp_risk.config import load_config
from perp_risk.liquidations import NullLiquidationSource
from perp_risk.models import PerpObservation, ProviderStatus
from perp_risk.providers import ProviderFetch
from perp_risk.service import RiskService
from perp_risk.store import RiskStore

NOW = datetime(2026, 7, 30, 14, 0, tzinfo=UTC)


class FakeProvider:
    def __init__(self, venue: str, observations: tuple[PerpObservation, ...]):
        self.venue = venue
        self.observations = observations

    def fetch(self, bindings: tuple[object, ...]) -> ProviderFetch:
        return ProviderFetch(
            observations=self.observations,
            status=ProviderStatus(
                venue=self.venue,
                status="ok",
                observation_count=len(self.observations),
            ),
        )

    def close(self) -> None:
        return None


def _provider_observations(
    venue: str,
    *,
    asof: datetime,
) -> tuple[PerpObservation, ...]:
    config = load_config()
    result = []
    for binding in config.bindings:
        if binding.venue != venue:
            continue
        result.append(
            PerpObservation(
                venue=venue,
                market=binding.market,
                instrument=binding.instrument,
                observed_at_utc=asof,
                mark_price=100,
                oracle_price=100,
                open_interest=100,
                funding_rate=0,
                notional_volume_24h=(
                    100_000_000 if binding.min_notional_volume_24h is not None else None
                ),
                bid_price=99.99,
                ask_price=100.01,
                aggressor_imbalance=0,
                aggressor_trade_count=10,
                provenance=f"fake:{venue}:{binding.instrument}",
            )
        )
    return tuple(result)


def test_service_persists_snapshot_and_latest_json(tmp_path: Path) -> None:
    original = load_config()
    storage = original.storage.model_copy(
        update={
            "database_path": str(tmp_path / "risk.sqlite3"),
            "latest_json_path": str(tmp_path / "latest.json"),
        }
    )
    config = original.model_copy(update={"storage": storage})
    store = RiskStore(config.database_path)
    service = RiskService(
        config=config,
        store=store,
        hyperliquid=FakeProvider(
            "hyperliquid",
            _provider_observations("hyperliquid", asof=NOW),
        ),  # type: ignore[arg-type]
        aevo=FakeProvider(
            "aevo",
            _provider_observations("aevo", asof=NOW),
        ),  # type: ignore[arg-type]
        liquidation_source=NullLiquidationSource(),
        clock=lambda: NOW,
    )

    try:
        result = service.run_snapshot(persist=True, notify=False)
    finally:
        service.close()

    latest = store.latest_snapshot()
    store.close()
    assert latest is not None
    assert latest.snapshot_id == result.snapshot.snapshot_id
    assert config.latest_json_path.is_file()
    assert result.snapshot.actionable is True
    assert result.snapshot.orders_submitted == 0
    assert "liquidation_provider_not_configured" in result.snapshot.warnings
