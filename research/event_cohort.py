"""Point-in-time catalyst cohort for the current 20:00 Beijing selection lock."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime

import polars as pl
from polars.datatypes import DataType, DataTypeClass

from data_plane.event_ledger import EventLedger
from kernel.catalysts import build_catalyst_candidates, select_overnight_catalysts
from research.history import premarket_decision_asof_utc

HARD_CATALYSTS = (
    "earnings",
    "contract_partnership",
    "regulatory_clinical",
    "merger_acquisition",
)
MEDIUM_CATALYSTS = ("other_material", "corporate_action")


def build_event_cohort(
    prepared_news: pl.DataFrame,
    *,
    schedule: pl.DataFrame,
    target_dates: Iterable[date],
) -> pl.DataFrame:
    """Build all eligible event symbols known by the fixed 20:00 Beijing lock."""
    rows: list[pl.DataFrame] = []
    for target in target_dates:
        decision_asof = premarket_decision_asof_utc(target)
        overnight = select_overnight_catalysts(
            prepared_news,
            schedule=schedule,
            target_date=target,
            asof_utc=decision_asof,
        )
        symbols = sorted(
            {
                str(symbol)
                for values in overnight.get_column("symbols").to_list()
                if isinstance(values, list)
                for symbol in values
            }
        )
        if not symbols:
            continue
        candidates = build_catalyst_candidates(
            pl.DataFrame({"symbol": symbols, "precheck_pass": [True] * len(symbols)}),
            overnight,
        )
        tier = (
            pl.when(
                pl.col("catalyst_categories")
                .list.eval(pl.element().is_in(HARD_CATALYSTS))
                .list.any()
            )
            .then(pl.lit(2))
            .when(
                pl.col("catalyst_categories")
                .list.eval(pl.element().is_in(MEDIUM_CATALYSTS))
                .list.any()
            )
            .then(pl.lit(1))
            .otherwise(pl.lit(0))
            .alias("catalyst_tier")
        )
        rows.append(
            candidates.with_columns(
                tier,
                pl.lit(decision_asof)
                .cast(pl.Datetime("ms", "UTC"))
                .alias("decision_asof_utc"),
            )
        )
    if not rows:
        return pl.DataFrame()
    return (
        pl.concat(rows, how="diagonal_relaxed")
        .unique(("session_date", "symbol"), keep="last")
        .sort("session_date", "symbol")
    )


def build_dynamic_event_cohort(ledger: EventLedger, *, asof_utc: datetime) -> pl.DataFrame:
    """All known event/symbol observations, including research-restricted sources.

    Source rows remain intact. Count distinct (event_cluster_id, symbol) for
    independent samples, not raw rows. No price, cap, outcome or universe fetches.
    """
    revisions = ledger.as_of(asof_utc, forward_only=False)
    schema: dict[str, DataType | DataTypeClass] = {
        "event_id": pl.String,
        "revision": pl.Int64,
        "event_cluster_id": pl.String,
        "body_hash": pl.String,
        "symbol": pl.String,
        "source": pl.String,
        "source_event_id": pl.String,
        "source_url": pl.String,
        "source_type": pl.String,
        "headline": pl.String,
        "summary": pl.String,
        "origin": pl.String,
        "published_at": pl.Datetime("us", "UTC"),
        "updated_at": pl.Datetime("us", "UTC"),
        "first_seen_at": pl.Datetime("us", "UTC"),
        "recorded_at": pl.Datetime("us", "UTC"),
        "available_at": pl.Datetime("us", "UTC"),
        "asof_utc": pl.Datetime("us", "UTC"),
        "forward_eligible": pl.Boolean,
        "coverage": pl.String,
        "missing_reason": pl.String,
        "market_cap": pl.Float64,
        "market_cap_status": pl.String,
        "tradable": pl.Boolean,
        "outcome_label": pl.String,
        "is_cluster_representative": pl.Boolean,
    }
    rows: list[dict[str, object]] = []
    clusters: set[tuple[str, str]] = set()
    for revision in sorted(revisions, key=lambda item: (item.available_at, item.event_id)):
        observation = revision.observation
        reasons = ["market_cap_missing", "outcome_not_computed", "trading_checks_not_evaluated"]
        if observation.first_seen_at is None:
            reasons.append("first_seen_missing")
        if observation.origin != "forward":
            reasons.append(f"{observation.origin}_origin")
        for symbol in observation.symbols:
            cluster = (revision.event_cluster_id, symbol)
            rows.append({
                "event_id": revision.event_id,
                "revision": revision.revision,
                "event_cluster_id": revision.event_cluster_id,
                "body_hash": revision.body_hash,
                "symbol": symbol,
                "source": observation.source,
                "source_event_id": observation.source_event_id,
                "source_url": observation.source_url,
                "source_type": observation.source_type,
                "headline": observation.headline,
                "summary": observation.summary,
                "origin": observation.origin,
                "published_at": observation.published_at,
                "updated_at": observation.updated_at,
                "first_seen_at": observation.first_seen_at,
                "recorded_at": revision.recorded_at,
                "available_at": revision.available_at,
                "asof_utc": asof_utc.astimezone(UTC),
                "forward_eligible": revision.forward_eligible,
                "coverage": "partial" if revision.forward_eligible else "research_only",
                "missing_reason": ";".join(reasons),
                "market_cap": None,
                "market_cap_status": "missing",
                "tradable": False,
                "outcome_label": None,
                "is_cluster_representative": cluster not in clusters,
            })
            clusters.add(cluster)
    return pl.DataFrame(rows, schema=schema).sort("symbol", "event_cluster_id", "event_id")
