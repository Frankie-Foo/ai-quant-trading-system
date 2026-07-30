"""Collect and persist shadow-only cross-asset perpetual sentiment."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import polars as pl

from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.providers.perpetual_sentiment import (
    AevoPerpClient,
    HyperliquidPerpClient,
    PerpInstrumentRequest,
    PerpProviderError,
)
from data_plane.storage import persist_snapshot
from kernel.cross_asset_sentiment import (
    CrossAssetSentimentEngine,
    CrossAssetSentimentPolicy,
    CrossAssetSentimentResult,
    PerpObservation,
    ProxyBinding,
)
from operations.cross_asset_sentiment_config import (
    CrossAssetSentimentConfig,
    load_cross_asset_sentiment_config,
)

ROOT = Path(__file__).resolve().parents[1]
RAW_SOURCE = "raw.cross_asset.perp_observations"
SENTIMENT_SOURCE = "kernel.cross_asset.sentiment_shadow"

_RAW_SCHEMA: dict[str, Any] = {
    "venue": pl.String,
    "market": pl.String,
    "instrument": pl.String,
    "observed_at_utc": pl.Datetime("ns", "UTC"),
    "mark_price": pl.Float64,
    "oracle_price": pl.Float64,
    "reference_price": pl.Float64,
    "open_interest": pl.Float64,
    "funding_rate": pl.Float64,
    "notional_volume_24h": pl.Float64,
    "bid_price": pl.Float64,
    "ask_price": pl.Float64,
    "aggressor_imbalance": pl.Float64,
    "aggressor_trade_count": pl.Int64,
    "long_liquidation_usd": pl.Float64,
    "short_liquidation_usd": pl.Float64,
    "liquidation_event_count": pl.Int64,
    "active": pl.Boolean,
    "provenance": pl.String,
}


@dataclass(frozen=True)
class CrossAssetSnapshotArtifacts:
    result: CrossAssetSentimentResult
    raw_frame: pl.DataFrame
    raw_snapshot: DatasetSnapshot
    raw_path: Path
    sentiment_frame: pl.DataFrame
    sentiment_snapshot: DatasetSnapshot
    sentiment_path: Path


class PerpObservationClient(Protocol):
    def fetch(
        self,
        instruments: tuple[PerpInstrumentRequest, ...],
    ) -> tuple[PerpObservation, ...]: ...

    def close(self) -> None: ...


def build_cross_asset_sentiment_snapshots(
    *,
    observations: tuple[PerpObservation, ...],
    previous_observations: tuple[PerpObservation, ...],
    bindings: tuple[ProxyBinding, ...],
    policy: CrossAssetSentimentPolicy,
    trade_date: date,
    asof_utc: datetime,
    data_root: Path,
    provider_status: dict[str, str],
    previous_snapshot_id: str | None = None,
) -> CrossAssetSnapshotArtifacts:
    """Evaluate once and persist raw evidence plus one target-level snapshot."""

    raw_frame = _raw_frame(observations)
    raw_provenance = f"{RAW_SOURCE}@{asof_utc.isoformat()}"
    duplicate_count = raw_frame.height - raw_frame.select(
        pl.struct(["venue", "market", "instrument"]).n_unique()
    ).item()
    future_count = raw_frame.filter(pl.col("observed_at_utc") > asof_utc).height
    raw_checks = (
        _check(
            "unique_venue_instrument",
            duplicate_count == 0,
            duplicate_count,
            "0 duplicate venue instruments",
            raw_provenance,
        ),
        _check(
            "point_in_time_cutoff",
            future_count == 0,
            future_count,
            "0 observations after declared asof",
            raw_provenance,
        ),
        DataQualityCheck(
            name="provider_coverage",
            severity=QualitySeverity.WARNING,
            passed=all(value == "ok" for value in provider_status.values()),
            observed=json.dumps(provider_status, sort_keys=True),
            expected="all configured providers report ok",
            provenance=raw_provenance,
        ),
    )
    raw_snapshot, raw_path = persist_snapshot(
        raw_frame,
        root=data_root,
        source=RAW_SOURCE,
        schema_version="perp_observations.v1",
        checks=raw_checks,
    )
    raw_snapshot.assert_usable()

    engine = CrossAssetSentimentEngine(policy=policy, bindings=bindings)
    result = engine.evaluate(
        observations=observations,
        previous_observations=previous_observations,
        asof_utc=asof_utc,
    )
    sentiment_frame = _sentiment_frame(
        result,
        trade_date=trade_date,
        provider_status=provider_status,
    )
    sentiment_provenance = f"{SENTIMENT_SOURCE}@{asof_utc.isoformat()}"
    expected_targets = {binding.target_id for binding in bindings}
    actual_targets = set(sentiment_frame.get_column("target_id").to_list())
    sentiment_duplicates = (
        sentiment_frame.height
        - sentiment_frame.get_column("target_id").n_unique()
    )
    future_targets = sentiment_frame.filter(
        pl.col("data_cutoff_utc") > asof_utc
    ).height
    production_count = sentiment_frame.filter(
        pl.col("production_eligible") | pl.col("execution_eligible")
    ).height
    sentiment_checks = (
        _check(
            "exact_configured_targets",
            actual_targets == expected_targets,
            sorted(actual_targets),
            str(sorted(expected_targets)),
            sentiment_provenance,
        ),
        _check(
            "unique_target",
            sentiment_duplicates == 0,
            sentiment_duplicates,
            "0 duplicate targets",
            sentiment_provenance,
        ),
        _check(
            "point_in_time_cutoff",
            future_targets == 0,
            future_targets,
            "0 target rows after declared asof",
            sentiment_provenance,
        ),
        _check(
            "shadow_only",
            production_count == 0,
            production_count,
            "0 production-eligible or execution-eligible rows",
            sentiment_provenance,
        ),
    )
    parent_ids = (raw_snapshot.dataset_id,) + (
        (previous_snapshot_id,) if previous_snapshot_id is not None else ()
    )
    sentiment_snapshot, sentiment_path = persist_snapshot(
        sentiment_frame,
        root=data_root,
        source=SENTIMENT_SOURCE,
        schema_version="cross_asset_sentiment_shadow.v1",
        checks=sentiment_checks,
        parent_snapshot_ids=parent_ids,
    )
    sentiment_snapshot.assert_usable()
    return CrossAssetSnapshotArtifacts(
        result=result,
        raw_frame=raw_frame,
        raw_snapshot=raw_snapshot,
        raw_path=raw_path,
        sentiment_frame=sentiment_frame,
        sentiment_snapshot=sentiment_snapshot,
        sentiment_path=sentiment_path,
    )


def collect_perp_observations(
    config: CrossAssetSentimentConfig,
    *,
    clients: dict[str, PerpObservationClient] | None = None,
) -> tuple[tuple[PerpObservation, ...], dict[str, str]]:
    """Collect all configured venues and degrade failures to explicit status."""

    owned = clients is None
    active_clients = clients or {
        "hyperliquid": HyperliquidPerpClient(
            flow_window_seconds=config.collection_interval_seconds,
        ),
        "aevo": AevoPerpClient(
            flow_window_seconds=config.collection_interval_seconds,
        ),
    }
    observations: list[PerpObservation] = []
    statuses: dict[str, str] = {}
    try:
        for venue in sorted({binding.venue for binding in config.bindings}):
            client = active_clients.get(venue)
            if client is None:
                statuses[venue] = "error:missing_adapter"
                continue
            requests = tuple(
                PerpInstrumentRequest(
                    market=market,
                    instrument=instrument,
                )
                for market, instrument in sorted(
                    {
                        (binding.market, binding.instrument)
                        for binding in config.bindings
                        if binding.venue == venue
                    }
                )
            )
            try:
                observations.extend(client.fetch(requests))
                statuses[venue] = "ok"
            except (PerpProviderError, OSError, ValueError) as exc:
                statuses[venue] = f"error:{type(exc).__name__}"
    finally:
        if owned:
            for client in active_clients.values():
                client.close()
    return (
        tuple(sorted(observations, key=lambda item: item.key)),
        statuses,
    )


def collect_observations_for_run(
    config: CrossAssetSentimentConfig,
    *,
    requested_asof_utc: datetime | None,
    clients: dict[str, PerpObservationClient] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> tuple[
    tuple[PerpObservation, ...],
    dict[str, str],
    datetime,
    str,
]:
    """Separate live collection from honest historical unavailability."""

    if requested_asof_utc is not None:
        return (
            (),
            {
                venue: "unavailable:historical_live_collection_forbidden"
                for venue in sorted(
                    {binding.venue for binding in config.bindings}
                )
            },
            requested_asof_utc,
            "historical_unavailable",
        )
    observations, provider_status = collect_perp_observations(
        config,
        clients=clients,
    )
    now_utc = (clock or (lambda: datetime.now(UTC)))()
    if now_utc.tzinfo is None or now_utc.utcoffset() != timedelta(0):
        raise ValueError("collection clock must return timezone-aware UTC")
    observation_cutoff = max(
        (item.observed_at_utc for item in observations),
        default=now_utc,
    )
    return (
        observations,
        provider_status,
        max(now_utc, observation_cutoff),
        "live",
    )


def load_previous_observations(
    data_root: Path,
    *,
    before_utc: datetime,
    history_snapshot_limit: int = 120,
) -> tuple[tuple[PerpObservation, ...], DatasetSnapshot | None]:
    """Load the latest prior usable raw observation snapshot."""

    candidates: list[
        tuple[datetime, Path, DatasetSnapshot, tuple[PerpObservation, ...]]
    ] = []
    paths = sorted(
        (data_root / "accepted").glob(f"{RAW_SOURCE}-*/data.parquet"),
        key=lambda item: item.parent.name,
        reverse=True,
    )[:history_snapshot_limit]
    for path in paths:
        manifest_path = path.parent / "manifest.json"
        try:
            snapshot = DatasetSnapshot.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            ).assert_usable()
            frame = pl.read_parquet(path)
            observations = tuple(
                PerpObservation.model_validate(row)
                for row in frame.iter_rows(named=True)
            )
        except (OSError, ValueError):
            continue
        if not observations:
            continue
        observation_cutoff = max(
            item.observed_at_utc for item in observations
        )
        if all(item.observed_at_utc < before_utc for item in observations):
            candidates.append(
                (observation_cutoff, path, snapshot, observations)
            )
    if not candidates:
        return (), None
    _, _path, snapshot, observations = max(
        candidates,
        key=lambda item: item[0],
    )
    return observations, snapshot


def _raw_frame(observations: tuple[PerpObservation, ...]) -> pl.DataFrame:
    rows = [
        observation.model_dump(mode="python")
        for observation in sorted(observations, key=lambda item: item.key)
    ]
    return pl.DataFrame(rows, schema=_RAW_SCHEMA, strict=False)


def _sentiment_frame(
    result: CrossAssetSentimentResult,
    *,
    trade_date: date,
    provider_status: dict[str, str],
) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for target in result.target_assessments:
        source_details = [
            item.model_dump(mode="json")
            for item in result.instrument_assessments
            if item.target_id == target.target_id
        ]
        rows.append(
            {
                "session_date": trade_date,
                "target_id": target.target_id,
                "scope": target.scope.value,
                "regime": target.regime.value,
                "score": target.score,
                "confidence": target.confidence,
                "coverage": target.coverage,
                "available_sources": target.available_sources,
                "configured_sources": target.configured_sources,
                "disagreement": target.disagreement,
                "source_scores_json": json.dumps(
                    dict(target.source_scores),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "source_provenance_json": json.dumps(
                    dict(target.source_provenance),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "source_details_json": json.dumps(
                    source_details,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "provider_status_json": json.dumps(
                    provider_status,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "data_cutoff_utc": result.asof_utc,
                "production_eligible": False,
                "execution_eligible": False,
                "provenance": (
                    f"{SENTIMENT_SOURCE}:{target.target_id}@"
                    f"{result.asof_utc.isoformat()}"
                ),
            }
        )
    return pl.DataFrame(rows).with_columns(
        pl.col("data_cutoff_utc").cast(pl.Datetime("ns", "UTC"))
    )


def _check(
    name: str,
    passed: bool,
    observed: object,
    expected: str,
    provenance: str,
) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=QualitySeverity.CRITICAL,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance=provenance,
    )


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _parse_asof(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "asof must be an ISO-8601 timestamp"
        ) from exc
    if result.tzinfo is None:
        raise argparse.ArgumentTypeError("asof must include a timezone")
    return result.astimezone(UTC)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_parse_date, required=True)
    parser.add_argument("--asof-utc", type=_parse_asof)
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "cross_asset_sentiment.yaml",
    )
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    args = parser.parse_args()

    config = load_cross_asset_sentiment_config(args.config)
    requested_asof_utc = args.asof_utc
    observations, provider_status, asof_utc, collection_mode = (
        collect_observations_for_run(
            config,
            requested_asof_utc=requested_asof_utc,
        )
    )
    if (
        asof_utc.tzinfo is None
        or asof_utc.utcoffset() != timedelta(0)
    ):
        raise ValueError("asof must be timezone-aware UTC")
    previous, previous_snapshot = load_previous_observations(
        args.data_root,
        before_utc=asof_utc,
        history_snapshot_limit=config.history_snapshot_limit,
    )
    artifacts = build_cross_asset_sentiment_snapshots(
        observations=observations,
        previous_observations=previous,
        bindings=config.bindings,
        policy=config.policy,
        trade_date=args.trade_date,
        asof_utc=asof_utc,
        data_root=args.data_root,
        provider_status=provider_status,
        previous_snapshot_id=(
            None
            if previous_snapshot is None
            else previous_snapshot.dataset_id
        ),
    )
    print(
        json.dumps(
            {
                "ok": True,
                "status": "shadow_complete",
                "trade_date": args.trade_date.isoformat(),
                "asof_utc": asof_utc.isoformat(),
                "collection_mode": collection_mode,
                "provider_status": provider_status,
                "observations": len(observations),
                "targets": len(artifacts.result.target_assessments),
                "available_targets": sum(
                    item.score is not None
                    for item in artifacts.result.target_assessments
                ),
                "production_eligible": False,
                "execution_eligible": False,
                "orders_submitted": 0,
                "raw_dataset_id": artifacts.raw_snapshot.dataset_id,
                "raw_path": str(artifacts.raw_path),
                "dataset_id": artifacts.sentiment_snapshot.dataset_id,
                "path": str(artifacts.sentiment_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
