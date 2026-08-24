"""Secret-free loader for autonomous Alpaca Paper session plans."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from execution.autonomous_paper_session import AutonomousPaperPlan
from kernel.intraday_policy import DecisionMetric, EntryRoute
from operations.autonomous_policy_adapter import AutonomousPolicyEvidence

SCHEMA_VERSION = "autonomous_paper_config.v1"


@dataclass(frozen=True)
class AutonomousPaperPlanBundle:
    plan: AutonomousPaperPlan
    evidence: AutonomousPolicyEvidence
    safety_envelope_path: Path
    benchmark_symbol: str
    sector_symbol: str
    market_context_provenance: str

    def __post_init__(self) -> None:
        for name, value in (
            ("benchmark_symbol", self.benchmark_symbol),
            ("sector_symbol", self.sector_symbol),
        ):
            if not value or value != value.strip().upper():
                raise ValueError(f"{name} must be normalized uppercase")
        if not self.market_context_provenance.strip():
            raise ValueError("market context provenance is required")


@dataclass(frozen=True)
class AutonomousPaperRuntimeConfig:
    poll_seconds: float
    plans: tuple[AutonomousPaperPlanBundle, ...]


def load_autonomous_paper_config(path: Path) -> AutonomousPaperRuntimeConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("autonomous Paper config is unreadable") from exc
    root = _object(raw, name="autonomous Paper config")
    _exact_keys(
        root,
        expected={"schema_version", "poll_seconds", "plans"},
        name="autonomous Paper config",
    )
    if root["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported autonomous Paper config schema")
    poll_seconds = float(root["poll_seconds"])
    if not 1 <= poll_seconds <= 60:
        raise ValueError("poll_seconds must be in [1, 60]")
    raw_plans = root["plans"]
    if not isinstance(raw_plans, list) or not raw_plans:
        raise ValueError("autonomous Paper config requires at least one plan")
    bundles: list[AutonomousPaperPlanBundle] = []
    seen_ids: set[str] = set()
    seen_symbols: set[str] = set()
    for index, raw_bundle in enumerate(raw_plans):
        bundle = _object(raw_bundle, name=f"plans[{index}]")
        _exact_keys(
            bundle,
            expected={
                "plan",
                "policy_evidence",
                "market_context",
                "safety_envelope",
            },
            name=f"plans[{index}]",
        )
        plan = _load_plan(_object(bundle["plan"], name=f"plans[{index}].plan"))
        if plan.plan_id in seen_ids:
            raise ValueError(f"duplicate autonomous Paper plan ID: {plan.plan_id}")
        if plan.symbol in seen_symbols:
            raise ValueError(f"duplicate autonomous Paper symbol: {plan.symbol}")
        evidence = _load_evidence(
            _object(
                bundle["policy_evidence"],
                name=f"plans[{index}].policy_evidence",
            )
        )
        raw_envelope = bundle["safety_envelope"]
        if not isinstance(raw_envelope, str) or not raw_envelope.strip():
            raise ValueError("safety_envelope must be a non-empty path")
        envelope_path = (path.parent / raw_envelope).resolve()
        market_context = _object(
            bundle["market_context"],
            name=f"plans[{index}].market_context",
        )
        _exact_keys(
            market_context,
            expected={"benchmark_symbol", "sector_symbol", "provenance"},
            name=f"plans[{index}].market_context",
        )
        bundles.append(
            AutonomousPaperPlanBundle(
                plan=plan,
                evidence=evidence,
                safety_envelope_path=envelope_path,
                benchmark_symbol=str(
                    market_context["benchmark_symbol"]
                ).strip().upper(),
                sector_symbol=str(
                    market_context["sector_symbol"]
                ).strip().upper(),
                market_context_provenance=str(market_context["provenance"]),
            )
        )
        seen_ids.add(plan.plan_id)
        seen_symbols.add(plan.symbol)
    return AutonomousPaperRuntimeConfig(
        poll_seconds=poll_seconds,
        plans=tuple(bundles),
    )


def _load_plan(values: dict[str, Any]) -> AutonomousPaperPlan:
    required = {
            "plan_id",
            "symbol",
            "trade_date",
            "reference_price",
            "hard_stop",
            "max_notional_fraction",
            "full_risk_fraction",
            "max_spread_ratio",
            "source_snapshot_ids",
            "provenance",
    }
    optional = {"take_profit_1", "take_profit_2"}
    unexpected = set(values) - required - optional
    missing = required - set(values)
    if unexpected:
        raise ValueError("autonomous Paper plan has unexpected fields")
    if missing:
        raise ValueError("autonomous Paper plan is missing required fields")
    source_ids = values["source_snapshot_ids"]
    if not isinstance(source_ids, list) or not all(
        isinstance(item, str) for item in source_ids
    ):
        raise ValueError("plan source_snapshot_ids must be strings")
    return AutonomousPaperPlan(
        plan_id=str(values["plan_id"]),
        symbol=str(values["symbol"]).strip().upper(),
        trade_date=date.fromisoformat(str(values["trade_date"])),
        reference_price=Decimal(str(values["reference_price"])),
        hard_stop=Decimal(str(values["hard_stop"])),
        max_notional_fraction=Decimal(str(values["max_notional_fraction"])),
        full_risk_fraction=Decimal(str(values["full_risk_fraction"])),
        source_snapshot_ids=tuple(source_ids),
        provenance=str(values["provenance"]),
        max_spread_ratio=Decimal(str(values["max_spread_ratio"])),
        take_profit_1=_optional_decimal(values.get("take_profit_1"), name="take_profit_1"),
        take_profit_2=_optional_decimal(values.get("take_profit_2"), name="take_profit_2"),
    )


def _optional_decimal(value: object, *, name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{name} is invalid") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{name} must be finite and positive when available")
    return parsed


def _load_evidence(values: dict[str, Any]) -> AutonomousPolicyEvidence:
    _exact_keys(
        values,
        expected={
            "route",
            "catalyst",
            "factor",
            "right_tail",
            "first_target_reward_r",
            "weighted_expected_reward_r",
            "reward_risk_provenance",
            "a_plus_plus_approved",
        },
        name="autonomous policy evidence",
    )
    approved = values["a_plus_plus_approved"]
    if not isinstance(approved, bool):
        raise ValueError("a_plus_plus_approved must be boolean")
    return AutonomousPolicyEvidence(
        route=EntryRoute(str(values["route"])),
        catalyst=_load_metric(values["catalyst"], name="catalyst"),
        factor=_load_metric(values["factor"], name="factor"),
        right_tail=_load_metric(values["right_tail"], name="right_tail"),
        first_target_reward_r=float(values["first_target_reward_r"]),
        weighted_expected_reward_r=float(
            values["weighted_expected_reward_r"]
        ),
        reward_risk_provenance=str(values["reward_risk_provenance"]),
        a_plus_plus_approved=approved,
    )


def _load_metric(value: object, *, name: str) -> DecisionMetric:
    values = _object(value, name=f"{name} metric")
    _exact_keys(
        values,
        expected={"value", "asof_utc", "provenance"},
        name=f"{name} metric",
    )
    raw_value = values["value"]
    return DecisionMetric(
        value=None if raw_value is None else float(raw_value),
        asof_utc=datetime.fromisoformat(str(values["asof_utc"])),
        provenance=str(values["provenance"]),
    )


def _object(value: object, *, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return cast(dict[str, Any], value)


def _exact_keys(
    values: dict[str, Any],
    *,
    expected: set[str],
    name: str,
) -> None:
    unexpected = set(values) - expected
    missing = expected - set(values)
    if unexpected:
        raise ValueError(f"{name} has unexpected fields")
    if missing:
        raise ValueError(f"{name} is missing required fields")
