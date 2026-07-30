"""Outcome review and human-gated threshold challenger generation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .config import AppConfig
from .models import Regime, ReviewReport
from .store import RiskStore


@dataclass(frozen=True)
class ThresholdCandidate:
    risk_on: float
    risk_off: float
    directional_accuracy: float
    overlay_contribution_pct: float


def review_outcomes(store: RiskStore) -> ReviewReport:
    rows = store.outcomes_with_snapshots()
    directional: list[bool] = []
    overlay: list[float] = []
    trades: list[float] = []
    benchmark_count = 0
    trade_count = 0
    warnings: list[str] = []
    for outcome, snapshot in rows:
        target = next(item for item in snapshot.targets if item.target_id == outcome.target_id)
        if outcome.kind == "benchmark":
            benchmark_count += 1
            overlay.append((target.effective_multiplier - 1) * outcome.return_pct)
            if target.regime is Regime.RISK_ON:
                directional.append(outcome.return_pct > 0)
            elif target.regime is Regime.RISK_OFF:
                directional.append(outcome.return_pct < 0)
        else:
            trade_count += 1
            trades.append(outcome.return_pct)
    if benchmark_count < 100:
        warnings.append("insufficient_benchmark_outcomes_for_config_challenger")
    return ReviewReport(
        generated_at_utc=datetime.now(UTC),
        outcome_count=len(rows),
        benchmark_count=benchmark_count,
        trade_count=trade_count,
        directional_samples=len(directional),
        directional_accuracy=(None if not directional else sum(directional) / len(directional)),
        average_overlay_contribution_pct=(None if not overlay else sum(overlay) / len(overlay)),
        average_trade_return_pct=(None if not trades else sum(trades) / len(trades)),
        warnings=tuple(warnings),
    )


def propose_threshold_config(
    *,
    store: RiskStore,
    config: AppConfig,
    output: Path,
) -> tuple[Path, dict[str, object]]:
    samples: list[tuple[float, float]] = []
    for outcome, snapshot in store.outcomes_with_snapshots():
        if outcome.kind != "benchmark":
            continue
        target = next(item for item in snapshot.targets if item.target_id == outcome.target_id)
        if target.score is not None:
            samples.append((target.score, outcome.return_pct))
    if len(samples) < 100:
        raise ValueError("at least 100 benchmark outcomes are required for a challenger")
    candidates: list[ThresholdCandidate] = []
    for risk_on in (20.0, 25.0, 30.0):
        for risk_off in (-20.0, -25.0, -30.0):
            correct: list[bool] = []
            overlay: list[float] = []
            for score, return_pct in samples:
                if score >= risk_on:
                    correct.append(return_pct > 0)
                    multiplier = 1.0
                elif score <= risk_off:
                    correct.append(return_pct < 0)
                    multiplier = config.policy.risk_off_multiplier
                else:
                    multiplier = 1.0
                overlay.append((multiplier - 1) * return_pct)
            candidates.append(
                ThresholdCandidate(
                    risk_on=risk_on,
                    risk_off=risk_off,
                    directional_accuracy=(sum(correct) / len(correct) if correct else 0),
                    overlay_contribution_pct=sum(overlay) / len(overlay),
                )
            )
    best = max(
        candidates,
        key=lambda item: (
            item.overlay_contribution_pct,
            item.directional_accuracy,
            -abs(item.risk_on - config.policy.risk_on_threshold),
            -abs(item.risk_off - config.policy.risk_off_threshold),
        ),
    )
    policy = config.policy.model_copy(
        update={
            "risk_on_threshold": best.risk_on,
            "risk_off_threshold": best.risk_off,
        }
    )
    challenger = config.model_copy(update={"policy": policy})
    payload = challenger.model_dump(mode="json")
    yaml_text = yaml.safe_dump(
        payload,
        sort_keys=False,
        allow_unicode=True,
    )
    candidate_hash = challenger.config_hash
    report: dict[str, object] = {
        "status": "candidate_only",
        "sample_count": len(samples),
        "attempted_configurations": len(candidates),
        "selected": {
            "risk_on_threshold": best.risk_on,
            "risk_off_threshold": best.risk_off,
            "directional_accuracy": best.directional_accuracy,
            "average_overlay_contribution_pct": (best.overlay_contribution_pct),
        },
        "candidate_hash": candidate_hash,
        "source_config_hash": config.config_hash,
        "production_eligible": False,
    }
    destination = output.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(yaml_text, encoding="utf-8")
    destination.with_suffix(destination.suffix + ".report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    store.save_config_candidate(
        candidate_hash=candidate_hash,
        source_config_hash=config.config_hash,
        candidate_yaml=yaml_text,
        report=report,
    )
    return destination, report


def approve_candidate(
    *,
    store: RiskStore,
    candidate_path: Path,
    destination: Path,
    confirmation_hash: str,
) -> Path:
    candidate = AppConfig.model_validate(yaml.safe_load(candidate_path.read_text(encoding="utf-8")))
    if candidate.config_hash != confirmation_hash:
        raise ValueError("confirmation hash does not match candidate")
    if store.config_candidate_status(confirmation_hash) != "candidate":
        raise ValueError("configuration candidate is unavailable")
    target = destination.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        candidate_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    temporary.replace(target)
    store.approve_config_candidate(confirmation_hash)
    return target
