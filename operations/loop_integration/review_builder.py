from __future__ import annotations

import hashlib
import json
import math
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from data_plane.contracts import DatasetSnapshot
from data_plane.storage import sha256_file
from kernel.config import Config
from kernel.strategy_policy import StrategyPolicy

from .contracts import (
    QuantReviewEnvelope,
    ReviewDecision,
    ReviewProvenance,
    StrategyIdentity,
)


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _git_commit(project_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def load_accepted_snapshot(path: Path) -> tuple[DatasetSnapshot, pl.DataFrame]:
    manifest_path = path.parent / "manifest.json"
    snapshot = DatasetSnapshot.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    ).assert_usable()
    if snapshot.dataset_id != path.parent.name:
        raise ValueError("accepted snapshot directory does not match dataset id")
    if sha256_file(path) != snapshot.content_sha256:
        raise ValueError("accepted snapshot content hash mismatch")
    return snapshot, pl.read_parquet(path)


def _verdict(row: dict[str, Any]) -> str:
    root_cause = str(row.get("root_cause") or "")
    if row.get("selection_status") == "selected":
        return "accept"
    if root_cause in {"intentional_gate", "incomplete_evidence"}:
        return "block"
    if root_cause == "late_catalyst":
        return "reject"
    return "watch"


def build_review_envelope(
    *,
    project_root: Path,
    trade_date: date,
    opportunity_path: Path,
    opportunity_snapshot: DatasetSnapshot,
    artifact_ids: tuple[str, ...],
    cfg: Config,
    active_policy: StrategyPolicy,
    strategy_id: str = "modern-h15",
    strategy_version: str = "modern-h15-v1",
    market_scope: str = "US-equity",
    market_regime: str = "UNKNOWN",
    execution_summary: dict[str, Any] | None = None,
    synthetic: bool = False,
) -> QuantReviewEnvelope:
    opportunity_snapshot.assert_usable()
    if sha256_file(opportunity_path) != opportunity_snapshot.content_sha256:
        raise ValueError("opportunity review hash mismatch")
    frame = pl.read_parquet(opportunity_path).sort("opportunity_rank", "symbol")
    required = {
        "session_date",
        "selection_cutoff_utc",
        "opportunity_rank",
        "symbol",
        "selection_status",
        "root_cause",
        "root_cause_detail",
        "pattern_key",
        "close_return",
        "mfe_from_previous_close",
        "mae_from_previous_close",
        "dollar_volume",
        "atr_pct",
        "provenance",
    }
    if missing := required - set(frame.columns):
        raise ValueError(f"opportunity review fields missing: {sorted(missing)}")
    if frame.height < 10:
        raise ValueError("Loop review requires at least ten research candidates")
    dates = frame.get_column("session_date").unique().to_list()
    if dates != [trade_date]:
        raise ValueError("opportunity review trade date mismatch")
    top = frame.head(10)
    cutoff = top.get_column("selection_cutoff_utc").max()
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None:
        raise ValueError("selection cutoff must be timezone-aware")
    as_of = opportunity_snapshot.asof_utc
    source_ids = tuple(dict.fromkeys((*artifact_ids, opportunity_snapshot.dataset_id)))
    decisions: list[ReviewDecision] = []
    for rank, row in enumerate(top.iter_rows(named=True), start=1):
        features: dict[str, float | int | str | bool | None] = {
            "close_return": _finite(row.get("close_return")),
            "mfe_from_previous_close": _finite(row.get("mfe_from_previous_close")),
            "mae_from_previous_close": _finite(row.get("mae_from_previous_close")),
            "dollar_volume": _finite(row.get("dollar_volume")),
            "atr_pct": _finite(row.get("atr_pct")),
            "selection_status": str(row.get("selection_status") or "unknown"),
            "root_cause": str(row.get("root_cause") or "unknown"),
            "path_status": "unavailable_not_materialized",
        }
        root_cause = str(row.get("root_cause") or "unknown")
        decisions.append(
            ReviewDecision(
                instrument=str(row["symbol"]).upper(),
                rank=rank,
                verdict=_verdict(row),  # type: ignore[arg-type]
                reason=str(row.get("root_cause_detail") or root_cause),
                event_time=cutoff,
                available_at=as_of,
                features=features,
                one_minute_path=(),
                trigger_results={
                    "pattern_key": str(row.get("pattern_key") or "unknown"),
                    "point_in_time_attribution": True,
                    "one_minute_path_available": False,
                },
                risk_controls=(
                    "LONG_ONLY",
                    "PAPER_ONLY",
                    f"daily_loss_limit={cfg.guardrails.daily_loss_limit}",
                    f"max_gross_exposure={cfg.max_gross_exposure}",
                ),
                invalidation_conditions=(
                    "source_snapshot_hash_mismatch",
                    "future_information_detected",
                    f"root_cause_changes:{root_cause}",
                ),
                source_snapshot_ids=(opportunity_snapshot.dataset_id,),
            )
        )
    top_returns = [value for value in top["close_return"].to_list() if _finite(value) is not None]
    remaining_returns = [
        value for value in frame.slice(10)["close_return"].to_list() if _finite(value) is not None
    ]
    top_atr = sorted(
        value
        for raw in top["atr_pct"].to_list()
        if (value := _finite(raw)) is not None and value > 0
    )
    if not top_atr:
        raise ValueError("Top10 ATR evidence is unavailable")
    median_atr_pct = top_atr[len(top_atr) // 2]
    stop_threshold_pct = median_atr_pct * cfg.exits.k_sl * 100
    config_hash = hashlib.sha256((project_root / "config.yaml").read_bytes()).hexdigest()
    risk_policy = {
        "position_limits": {
            "risk_per_trade_fraction": cfg.risk_per_trade,
            "max_concurrent": float(cfg.max_concurrent),
            "max_gross_exposure_fraction": cfg.max_gross_exposure,
        },
        "stop_loss": {
            "type": "top10_median_atr_multiple",
            "threshold_pct": stop_threshold_pct,
            "reference": "median(top10.atr_pct) * kernel.exits.k_sl",
        },
        "exit_conditions": [
            f"time_stop_et={cfg.exits.time_stop_et}",
            "deterministic_stop_loss",
        ],
        "invalidation_conditions": [
            "market_data_unhealthy",
            "point_in_time_guard_failed",
            "snapshot_quarantined",
        ],
        "blocking_conditions": ["kill_switch", "not_paper", "stale_sip_quote"],
        "reentry_conditions": ["local_strategy_policy_only"],
        "risk_budget": {"daily_loss_limit_fraction": cfg.guardrails.daily_loss_limit},
        "liquidity_constraints": {"participation_cap": cfg.participation_cap},
        "evidence": {
            "source": "ai-quant-trading-system/config.yaml",
            "effective_at": cutoff.isoformat(),
            "available_at": as_of.isoformat(),
            "config_sha256": config_hash,
        },
    }
    return QuantReviewEnvelope(
        event_id=(
            f"quant-review:{market_scope}:{trade_date.isoformat()}:"
            f"{strategy_id}:{active_policy.policy_hash[:16]}"
        ),
        trading_date=trade_date,
        market_scope=market_scope,
        as_of=as_of,
        strategy=StrategyIdentity(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            active_policy_version=active_policy.version,
            active_policy_hash=active_policy.policy_hash,
        ),
        provenance=ReviewProvenance(
            synthetic=synthetic,
            not_real_market_data=synthetic,
            code_commit=_git_commit(project_root),
            config_sha256=config_hash,
            source_snapshot_ids=source_ids,
            feature_schema_versions=(opportunity_snapshot.schema_version,),
            cost_model_version="kernel.quote_costs.v1",
            created_at_utc=datetime.now(UTC),
        ),
        market_context={"regime": market_regime},
        top10_decisions=tuple(decisions),
        execution_summary={"orders_authorized": False, **(execution_summary or {})},
        risk_policy=risk_policy,
        metrics={
            "top10_pnl": sum(float(value) for value in top_returns),
            "non_top10_pnl": sum(float(value) for value in remaining_returns),
            "ab_hit_rate": (
                sum(float(value) > 0 for value in top_returns) / len(top_returns)
                if top_returns
                else 0.0
            ),
        },
        conclusions=(
            "Daily review evidence was generated from accepted immutable snapshots.",
            "Missing one-minute paths remain unavailable and were not fabricated.",
            "Loop output is advisory and cannot authorize broker orders.",
        ),
    )


def envelope_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
