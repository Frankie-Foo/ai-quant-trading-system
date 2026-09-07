from __future__ import annotations

import hashlib
import json
import math
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, cast

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
from .execution_summary import (
    RiskEvidenceUnavailable,
    build_factual_execution_summary,
    load_effective_plan,
    unavailable_execution,
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


DecisionAction = Literal["accept", "watch", "reject", "block"]


def _verdict(row: dict[str, Any]) -> DecisionAction:
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
    strategy_version: str | None = None,
    market_scope: str = "US-equity",
    market_regime: str = "UNKNOWN",
    execution_summary: dict[str, Any] | None = None,
    effective_plan_path: Path | None = None,
    effective_plan_sha256: str | None = None,
    fill_evidence_path: Path | None = None,
    fill_evidence_sha256: str | None = None,
    review_context_path: Path | None = None,
    review_context_sha256: str | None = None,
    synthetic: bool = False,
) -> QuantReviewEnvelope:
    # Generic kernel configuration is not evidence of the effective modern plan.
    del cfg
    if execution_summary is not None:
        raise ValueError("execution summary requires frozen plan and confirmed fill evidence")
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
        "classification",
        "classification_source",
        "logging_policy_id",
        "logged_action",
        "logging_action_probability",
        "reward_model_logged",
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
    risk_policy: dict[str, Any] = {
        "status": "unavailable",
        "reason": "effective_modern_plan_not_supplied",
        "submission_allowed": False,
    }
    frozen_pool: dict[str, Any] = {
        "status": "unavailable", "count": None, "candidates": None,
        "semantics": "complete_frozen_morning_pool",
    }
    factual_execution = {"orders_authorized": False, **unavailable_execution()}
    if (effective_plan_path is None) != (effective_plan_sha256 is None):
        raise ValueError("effective plan path and pinned hash must be supplied together")
    if effective_plan_path is None and (
        fill_evidence_path or fill_evidence_sha256 or review_context_path or review_context_sha256
    ):
        raise ValueError("broker fill evidence requires a frozen effective plan")
    plan = None
    if effective_plan_path is not None and effective_plan_sha256 is not None:
        try:
            plan = load_effective_plan(
                effective_plan_path, expected_sha256=effective_plan_sha256,
                trade_date=trade_date, as_of=as_of, require_native_evidence=True,
                review_context_path=review_context_path,
                review_context_sha256=review_context_sha256,
            )
        except RiskEvidenceUnavailable as exc:
            risk_policy["reason"] = str(exc)
    if plan is not None and effective_plan_path is not None and effective_plan_sha256 is not None:
        if (
            plan.strategy.strategy_id != strategy_id
            or (strategy_version is not None and plan.strategy.strategy_version != strategy_version)
            or plan.strategy.active_policy_hash != active_policy.policy_hash
            or plan.selection_cutoff_utc != cutoff
        ):
            raise ValueError("effective plan strategy/config/selection cutoff mismatch")
        strategy_version = plan.strategy.strategy_version
        risk = plan.strategy.risk_policy
        risk_policy = {
            "status": "available",
            "submission_allowed": True,
            "position_limits": {"risk_per_trade_fraction": risk.symbol_risk_fraction},
            "stop_loss": {
                "type": "maximum_all_in_stop",
                "threshold_pct": risk.maximum_all_in_stop_pct * 100,
            },
            "exit_conditions": [
                f"no_new_entry_et={risk.new_entry_cutoff_et}", f"flatten_et={risk.flatten_et}",
            ],
            "attempt_weights": list(risk.attempt_weights),
            "liquidity_constraints": {
                "maximum_entry_relative_spread": plan.strategy.parameters[
                    "maximum_entry_relative_spread"
                ],
            },
            "evidence": {
                "source": str(effective_plan_path),
                "plan_sha256": effective_plan_sha256,
                "strategy_sha256": plan.strategy_sha256,
                "review_context_sha256": review_context_sha256,
                "authorization_strategy_version": plan.authorization_strategy_version,
                "source_snapshot_ids": list(plan.source_snapshot_ids),
                "effective_at": plan.effective_at_utc.isoformat(),
                "available_at": plan.available_at_utc.isoformat(),
            },
        }
        if plan.candidates is not None:
            frozen_pool.update(
                status="available", count=len(plan.candidates),
                candidates=[item.model_dump(mode="json") for item in plan.candidates],
                source_snapshot_ids=list(plan.source_snapshot_ids),
                plan_sha256=effective_plan_sha256,
                available_at=plan.candidate_pool_available_at_utc.isoformat()
                if plan.candidate_pool_available_at_utc is not None else None,
            )
        factual_execution = build_factual_execution_summary(
            plan_path=effective_plan_path, plan_sha256=effective_plan_sha256,
            trade_date=trade_date, as_of=as_of,
            fills_path=fill_evidence_path, fills_sha256=fill_evidence_sha256,
            review_context_path=review_context_path, review_context_sha256=review_context_sha256,
        )
    source_ids = tuple(dict.fromkeys((*artifact_ids, opportunity_snapshot.dataset_id)))
    decisions: list[ReviewDecision] = []
    for rank, row in enumerate(top.iter_rows(named=True), start=1):
        features: dict[str, float | int | str | bool | None] = {
            "close_return": _finite(row.get("close_return")),
            "mfe_from_previous_close": _finite(row.get("mfe_from_previous_close")),
            "mae_from_previous_close": _finite(row.get("mae_from_previous_close")),
            "dollar_volume": _finite(row.get("dollar_volume")),
            "atr_pct": _finite(row.get("atr_pct")),
            "rvol": _finite(row.get("rvol")),
            "selection_status": str(row.get("selection_status") or "unknown"),
            "root_cause": str(row.get("root_cause") or "unknown"),
            "path_status": "unavailable_not_materialized",
        }
        root_cause = str(row.get("root_cause") or "unknown")
        classification = str(row.get("classification") or "").strip().upper()
        classification_source = str(row.get("classification_source") or "").strip()
        if not classification or not classification_source:
            raise ValueError("opportunity review classification contract is incomplete")
        verdict = _verdict(row)
        logging_policy_id = str(row.get("logging_policy_id") or "").strip()
        logged_action = str(row.get("logged_action") or "").strip().lower()
        logging_action_probability = _finite(row.get("logging_action_probability"))
        reward_model_logged = _finite(row.get("reward_model_logged"))
        if (
            not logging_policy_id
            or logged_action not in {"accept", "watch", "reject", "block"}
            or logging_action_probability is None
            or not 0.0 < logging_action_probability <= 1.0
            or reward_model_logged is None
        ):
            raise ValueError("opportunity review OPE contract is incomplete")
        typed_logged_action = cast(DecisionAction, logged_action)
        decisions.append(
            ReviewDecision(
                instrument=str(row["symbol"]).upper(),
                rank=rank,
                market_regime=market_regime,
                classification=classification,
                classification_source=classification_source,
                logging_policy_id=logging_policy_id,
                logged_action=typed_logged_action,
                logging_action_probability=logging_action_probability,
                reward_model_logged=reward_model_logged,
                verdict=verdict,
                reason=str(row.get("root_cause_detail") or root_cause),
                event_time=cutoff,
                available_at=as_of,
                features=features,
                one_minute_path=(),
                trigger_results={
                    "classification": classification,
                    "classification_source": classification_source,
                    "logging_policy_id": logging_policy_id,
                    "logged_action": logged_action,
                    "logging_action_probability": logging_action_probability,
                    "reward_model_logged": reward_model_logged,
                    "reward_model_id": str(
                        row.get("reward_model_id") or "zero_net_return_baseline.v1"
                    ),
                    "pattern_key": str(row.get("pattern_key") or "unknown"),
                    "point_in_time_attribution": True,
                    "one_minute_path_available": False,
                },
                risk_controls=(
                    "LONG_ONLY",
                    "PAPER_ONLY",
                    f"effective_modern_risk={risk_policy['status']}",
                ),
                invalidation_conditions=(
                    "source_snapshot_hash_mismatch",
                    "future_information_detected",
                    f"root_cause_changes:{root_cause}",
                ),
                source_snapshot_ids=(opportunity_snapshot.dataset_id,),
            )
        )
    top_returns = [
        value for raw in top["close_return"].to_list() if (value := _finite(raw)) is not None
    ]
    remaining_returns = [
        value
        for raw in frame.slice(10)["close_return"].to_list()
        if (value := _finite(raw)) is not None
    ]
    config_hash = hashlib.sha256((project_root / "config.yaml").read_bytes()).hexdigest()
    return QuantReviewEnvelope(
        event_id=(
            f"quant-review:{market_scope}:{trade_date.isoformat()}:"
            f"{strategy_id}:{active_policy.policy_hash[:16]}:evidence-v2:"
            f"{(effective_plan_sha256 or 'unavailable')[:16]}"
            f":{(fill_evidence_sha256 or 'unavailable')[:16]}"
            f":{(review_context_sha256 or 'none')[:16]}"
        ),
        trading_date=trade_date,
        market_scope=market_scope,
        as_of=as_of,
        strategy=StrategyIdentity(
            strategy_id=strategy_id,
            strategy_version=strategy_version or "unavailable",
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
            created_at_utc=as_of,
        ),
        market_context={
            "regime": market_regime,
            "market_regime": market_regime,
            "classification": decisions[0].classification,
            "classification_source": decisions[0].classification_source,
            "frozen_candidate_pool": frozen_pool,
            "post_close_winners": {
                "status": "available",
                "count": frame.height,
                "symbols": frame["symbol"].to_list(),
                "source_snapshot_id": opportunity_snapshot.dataset_id,
                "semantics": "after_close_opportunity_ranking_not_morning_candidate_pool",
            },
        },
        top10_decisions=tuple(decisions),
        execution_summary=factual_execution,
        risk_policy=risk_policy,
        metrics={
            "top10_close_return_sum": sum(top_returns),
            "non_top10_close_return_sum": sum(remaining_returns),
            "top10_positive_close_return_rate": (
                sum(value > 0 for value in top_returns) / len(top_returns) if top_returns else 0.0
            ),
            "top10_close_return_sample_count": len(top_returns),
            "non_top10_close_return_sample_count": len(remaining_returns),
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
