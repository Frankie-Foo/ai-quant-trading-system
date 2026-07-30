from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from data_plane.contracts import QualitySeverity
from kernel.cross_asset_sentiment import (
    CrossAssetSentimentPolicy,
    PerpObservation,
    ProxyBinding,
    SentimentScope,
)
from scripts.build_cross_asset_sentiment_snapshot import (
    build_cross_asset_sentiment_snapshots,
)

ASOF = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def _observation(
    *,
    venue: str,
    instrument: str,
    price: float,
    open_interest: float | None,
) -> PerpObservation:
    return PerpObservation(
        venue=venue,
        market="main" if venue == "hyperliquid" else "mainnet",
        instrument=instrument,
        observed_at_utc=ASOF,
        mark_price=price,
        oracle_price=price / 1.001,
        reference_price=price / 1.01,
        open_interest=open_interest,
        funding_rate=0.00004,
        notional_volume_24h=(
            500_000_000 if venue == "hyperliquid" else None
        ),
        active=True,
        provenance=f"{venue}.public@{ASOF.isoformat()}",
    )


def test_cross_asset_snapshots_are_auditable_and_shadow_only(
    tmp_path: Path,
) -> None:
    bindings = (
        ProxyBinding(
            target_id="global-risk",
            scope=SentimentScope.MARKET,
            venue="hyperliquid",
            market="main",
            instrument="BTC",
            weight=0.6,
            min_notional_volume_24h=1_000_000,
        ),
        ProxyBinding(
            target_id="global-risk",
            scope=SentimentScope.MARKET,
            venue="aevo",
            market="mainnet",
            instrument="BTC-PERP",
            weight=0.4,
        ),
    )
    artifacts = build_cross_asset_sentiment_snapshots(
        observations=(
            _observation(
                venue="hyperliquid",
                instrument="BTC",
                price=101,
                open_interest=1_100,
            ),
            _observation(
                venue="aevo",
                instrument="BTC-PERP",
                price=101,
                open_interest=None,
            ),
        ),
        previous_observations=(
            _observation(
                venue="hyperliquid",
                instrument="BTC",
                price=100,
                open_interest=1_000,
            ).model_copy(
                update={"observed_at_utc": ASOF - timedelta(minutes=5)}
            ),
        ),
        bindings=bindings,
        policy=CrossAssetSentimentPolicy(),
        trade_date=date(2026, 7, 30),
        asof_utc=ASOF + timedelta(seconds=1),
        data_root=tmp_path,
        provider_status={"hyperliquid": "ok", "aevo": "ok"},
    )

    assert artifacts.raw_snapshot.usable
    assert artifacts.sentiment_snapshot.usable
    assert artifacts.raw_path.exists()
    assert artifacts.sentiment_path.exists()
    assert artifacts.sentiment_snapshot.parent_snapshot_ids == (
        artifacts.raw_snapshot.dataset_id,
    )
    assert artifacts.sentiment_frame.get_column("target_id").to_list() == [
        "global-risk"
    ]
    assert artifacts.sentiment_frame.filter(
        artifacts.sentiment_frame["production_eligible"]
    ).is_empty()
    assert artifacts.sentiment_frame.filter(
        artifacts.sentiment_frame["execution_eligible"]
    ).is_empty()
    assert all(
        check.passed
        for check in artifacts.sentiment_snapshot.checks
        if check.severity is QualitySeverity.CRITICAL
    )
