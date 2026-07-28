"""Secret-free configuration loader for adaptive plan baselines."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from kernel.adaptive_trade_plan import BaselineTradePlan, PlanMode
from operations.adaptive_plan_adapters import PlanEvidence

SCHEMA_VERSION = "adaptive_plan_config.v1"


@dataclass(frozen=True)
class AdaptivePlanConfig:
    poll_seconds: int
    plans: tuple[BaselineTradePlan, ...]
    evidence: dict[str, PlanEvidence]


def load_adaptive_plan_config(path: Path) -> AdaptivePlanConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("adaptive plan config root must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported adaptive plan config schema")
    poll_seconds = int(payload.get("poll_seconds", 15))
    if not 5 <= poll_seconds <= 60:
        raise ValueError("poll_seconds must be in [5, 60]")
    raw_plans = payload.get("plans")
    if not isinstance(raw_plans, list) or not raw_plans:
        raise ValueError("adaptive plan config requires at least one plan")
    plans: list[BaselineTradePlan] = []
    evidence: dict[str, PlanEvidence] = {}
    for raw_item in raw_plans:
        if not isinstance(raw_item, dict):
            raise ValueError("adaptive plan entry must be an object")
        baseline = raw_item.get("baseline")
        raw_evidence = raw_item.get("evidence")
        if not isinstance(baseline, dict) or not isinstance(raw_evidence, dict):
            raise ValueError("plan baseline and evidence must be objects")
        baseline_values = cast(dict[str, Any], baseline)
        evidence_values = cast(dict[str, Any], raw_evidence)
        plan = BaselineTradePlan(
            plan_id=str(baseline_values["plan_id"]),
            symbol=str(baseline_values["symbol"]).strip().upper(),
            trade_date=date.fromisoformat(str(baseline_values["trade_date"])),
            mode=PlanMode(str(baseline_values["mode"])),
            entry_window_end_utc=datetime.fromisoformat(
                str(baseline_values["entry_window_end_utc"])
            ),
            force_exit_utc=datetime.fromisoformat(
                str(baseline_values["force_exit_utc"])
            ),
            hard_stop=float(baseline_values["hard_stop"]),
            max_risk_dollars=float(baseline_values["max_risk_dollars"]),
            max_notional=float(baseline_values["max_notional"]),
            probe_fraction=float(baseline_values["probe_fraction"]),
            max_spread_ratio=float(baseline_values["max_spread_ratio"]),
            soft_cooldown=timedelta(
                seconds=float(baseline_values["soft_cooldown_seconds"])
            ),
            max_soft_revisions=int(baseline_values["max_soft_revisions"]),
        )
        if plan.plan_id in evidence:
            raise ValueError(f"duplicate adaptive plan id: {plan.plan_id}")
        plan_evidence = PlanEvidence(
            benchmark_symbol=str(
                evidence_values["benchmark_symbol"]
            ).strip().upper(),
            sector_symbol=str(evidence_values["sector_symbol"]).strip().upper(),
            catalyst_score=(
                None
                if evidence_values.get("catalyst_score") is None
                else float(evidence_values["catalyst_score"])
            ),
            provenance=str(evidence_values["provenance"]),
        )
        plans.append(plan)
        evidence[plan.plan_id] = plan_evidence
    return AdaptivePlanConfig(
        poll_seconds=poll_seconds,
        plans=tuple(plans),
        evidence=evidence,
    )
