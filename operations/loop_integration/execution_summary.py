"""Read-only, hash-pinned modern plan and broker-fill evidence.

Input validation proves local integrity/lineage, not broker authenticity. The
producer must export confirmed execution events, not order intents or cumulative
order snapshots. See REVIEW_FIXES.md for the explicit offline input contract.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict, deque
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import ConfigDict, Field, field_validator, model_validator

from data_plane.calendar import build_xnys_schedule

from .contracts import FrozenModel

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Nonempty = Annotated[str, Field(min_length=1, pattern=r"\S")]
Symbol = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9.-]{0,15}$")]


class RiskEvidenceUnavailable(ValueError):
    """Required native risk facts are absent, not eligible for guessed defaults."""


class ExecutionEvidenceFiles(FrozenModel):
    """Runner file references, not a substitute for native or broker evidence."""

    trade_date: date
    strategy_sha256: Sha256
    plan_path: Path
    plan_sha256: Sha256
    fills_path: Path
    fills_sha256: Sha256
    review_context_path: Path | None = None
    review_context_sha256: Sha256 | None = None

    def attachment_args(self) -> dict[str, Any]:
        return self.model_dump(exclude={"trade_date", "strategy_sha256"})


def load_execution_index(
    path: Path | None, sha256: str | None,
) -> tuple[ExecutionEvidenceFiles, ...]:
    if (path is None) != (sha256 is None):
        raise ValueError("execution index path and hash must be paired")
    if path is None or sha256 is None:
        return ()
    raw = json.loads(read_pinned(path, sha256))
    entries = tuple(ExecutionEvidenceFiles.model_validate(row) for row in raw["executions"])
    if len({(row.trade_date, row.strategy_sha256) for row in entries}) != len(entries):
        raise ValueError("ambiguous execution index date/strategy identity")
    resolved = []
    for entry in entries:
        updates = {
            key: path.parent / value for key in ("plan_path", "fills_path", "review_context_path")
            if (value := getattr(entry, key)) is not None and not value.is_absolute()
        }
        resolved.append(entry.model_copy(update=updates))
    return tuple(resolved)


def canonical_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def read_pinned(path: Path, expected_sha256: str) -> bytes:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("evidence file hash mismatch")
    return raw


def write_pinned_json(directory: Path, label: str, payload: dict[str, Any]) -> tuple[Path, str]:
    """New content-addressed local artifact; existing evidence is never overwritten."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    digest = hashlib.sha256(raw).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory.resolve() / f"{label}-{digest}.json"
    try:
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        read_pinned(path, digest)
    return path, digest


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("evidence timestamps must be timezone-aware UTC")
    return value


class ModernRiskPolicy(FrozenModel):
    # Required evidence, not defaults. Limits are the reviewed modern-H15 ceiling.
    symbol_risk_fraction: float = Field(gt=0, le=0.005)
    maximum_all_in_stop_pct: float = Field(gt=0, le=0.02)
    new_entry_cutoff_et: Literal["15:00"]
    flatten_et: Literal["15:50"]
    attempt_weights: tuple[float, float]

    @field_validator("attempt_weights")
    @classmethod
    def reviewed_attempt_weights(cls, value: tuple[float, float]) -> tuple[float, float]:
        if value != (0.6, 0.4):
            raise ValueError("effective modern attempt weights must be explicitly 60/40")
        return value


class ModernStrategy(FrozenModel):
    strategy_id: Literal["modern-h15"]
    strategy_version: Nonempty
    active_policy_hash: Sha256
    parameters: dict[str, Any] = Field(min_length=1)
    risk_policy: ModernRiskPolicy


class FrozenCandidate(FrozenModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    symbol: Symbol
    verdict: Literal["accept", "watch", "reject", "block"] | None = None


class EffectiveModernPlan(FrozenModel):
    schema_version: Literal["loop_effective_modern_plan.v1"]
    trade_date: date
    strategy: ModernStrategy
    strategy_sha256: Sha256
    native_plan_sha256: Sha256 | None = None
    authorization_strategy_version: Nonempty | None = None
    effective_at_utc: datetime
    available_at_utc: datetime
    selection_cutoff_utc: datetime
    candidate_pool_available_at_utc: datetime | None = None
    source_snapshot_ids: tuple[Nonempty, ...] = Field(min_length=1)
    candidate_pool_complete: bool
    candidate_pool_source: Literal["complete_frozen_morning_pool"]
    candidates: tuple[FrozenCandidate, ...] | None

    _utc = field_validator(
        "effective_at_utc", "available_at_utc", "selection_cutoff_utc"
    )(require_utc)

    @field_validator("candidate_pool_available_at_utc")
    @classmethod
    def pool_availability(cls, value: datetime | None) -> datetime | None:
        return require_utc(value) if value is not None else None

    @model_validator(mode="after")
    def verify(self) -> Self:
        if canonical_sha256(self.strategy.model_dump(mode="json")) != self.strategy_sha256:
            raise ValueError("effective strategy config hash mismatch")
        cutoff_date = self.selection_cutoff_utc.astimezone(ZoneInfo("America/New_York")).date()
        if cutoff_date != self.trade_date:
            raise ValueError("plan cutoff trading date mismatch")
        if self.candidate_pool_complete != (self.candidates is not None):
            raise ValueError("candidate pool must be complete or explicitly unavailable")
        if self.candidates is not None:
            if (
                self.candidate_pool_available_at_utc is None
                or self.candidate_pool_available_at_utc > self.selection_cutoff_utc
            ):
                raise ValueError("morning pool availability must not follow its selection cutoff")
            symbols = [item.symbol for item in self.candidates]
            if len(set(symbols)) != len(symbols):
                raise ValueError("frozen candidate pool contains duplicate symbols")
        return self


def _native_manifest(raw: dict[str, Any]) -> dict[str, Any]:
    manifest = raw.get("modern_strategy_manifest")
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != "modern_strategy_manifest.v1"
    ):
        raise RiskEvidenceUnavailable("native modern strategy manifest unavailable")
    parameters = manifest.get("effective_config")
    if not isinstance(parameters, dict) or not parameters or not manifest.get("strategy_version"):
        raise RiskEvidenceUnavailable("native effective config/version unavailable")
    if canonical_sha256(parameters) != manifest.get("config_sha256"):
        raise ValueError("native manifest config hash mismatch")
    risk_keys = tuple(ModernRiskPolicy.model_fields)
    required = (*risk_keys, "maximum_entry_relative_spread", "target_r")
    if any(key not in raw for key in required):
        raise RiskEvidenceUnavailable("native explicit risk fields unavailable")
    ModernRiskPolicy.model_validate({key: raw[key] for key in risk_keys})
    comparisons = {
        "maximum_all_in_stop_pct": "max_all_in_stop_pct",
        "maximum_entry_relative_spread": "maximum_entry_relative_spread",
        "target_r": "target_r",
    }
    if any(parameters.get(config_key) != raw[key] for key, config_key in comparisons.items()):
        raise ValueError("native risk and manifest effective config mismatch")
    return manifest


class BrokerFill(FrozenModel):
    fill_id: Nonempty
    broker_order_id: Nonempty
    symbol: Symbol
    side: Literal["buy", "sell"]
    filled_at_utc: datetime
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    price: Decimal = Field(gt=0, allow_inf_nan=False)
    fees: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    fee_source: Nonempty | None = None
    source: Nonempty
    broker_confirmed: Literal[True]

    _utc = field_validator("filled_at_utc")(require_utc)

    @model_validator(mode="after")
    def fee_provenance(self) -> Self:
        if self.fees is not None and self.fee_source is None:
            raise ValueError("known fees require fee_source, including explicit zero fees")
        return self


class BrokerFillEvidence(FrozenModel):
    source_evidence: dict[str, Any] = Field(default_factory=dict)
    schema_version: Literal["loop_broker_fills.v1"]
    evidence_kind: Literal["broker_confirmed_fills"]
    quantity_semantics: Literal["incremental_execution"]
    trade_date: date
    strategy_sha256: Sha256
    plan_sha256: Sha256
    broker: Nonempty
    account_id: Nonempty
    environment: Literal["paper", "live"]
    currency: Literal["USD"]
    source: Nonempty
    validated_by: Nonempty
    coverage_start_utc: datetime
    coverage_end_utc: datetime
    opening_positions_flat: Literal[True]
    reconciled_complete: bool
    costs_complete: bool
    fills: tuple[BrokerFill, ...]

    _utc = field_validator("coverage_start_utc", "coverage_end_utc")(require_utc)

    @model_validator(mode="after")
    def coverage(self) -> Self:
        if self.coverage_start_utc >= self.coverage_end_utc:
            raise ValueError("invalid broker evidence coverage")
        if len({fill.fill_id for fill in self.fills}) != len(self.fills):
            raise ValueError("duplicate broker fill id")
        orders: dict[str, tuple[str, str]] = {}
        for fill in self.fills:
            identity = (fill.symbol, fill.side)
            if orders.setdefault(fill.broker_order_id, identity) != identity:
                raise ValueError("broker order id has inconsistent symbol/side")
            if not self.coverage_start_utc <= fill.filled_at_utc <= self.coverage_end_utc:
                raise ValueError("fill outside broker evidence coverage")
            fill_date = fill.filled_at_utc.astimezone(ZoneInfo("America/New_York")).date()
            if fill_date != self.trade_date:
                raise ValueError("fill trading date mismatch")
        if self.reconciled_complete:
            schedule = build_xnys_schedule(self.trade_date, self.trade_date)
            if schedule.height != 1:
                raise ValueError("broker evidence requires an XNYS session")
            session = schedule.row(0, named=True)
            if (
                self.coverage_start_utc > session["market_open_utc"]
                or self.coverage_end_utc < session["market_close_utc"]
            ):
                raise ValueError("complete broker evidence must cover the entire session")
        return self


def load_effective_plan(
    path: Path, *, expected_sha256: str, trade_date: date, as_of: datetime,
    review_context_path: Path | None = None, review_context_sha256: str | None = None,
    require_native_evidence: bool = False,
) -> EffectiveModernPlan:
    require_utc(as_of)
    raw = json.loads(read_pinned(path, expected_sha256))
    native = isinstance(raw, dict) and raw.get("schema_version") == "modern_h15_paper_plan.v1"
    if require_native_evidence and not native:
        raise RiskEvidenceUnavailable(
            "native risk/manifest evidence unavailable; sidecar is not proof"
        )
    if (review_context_path is None) != (review_context_sha256 is None):
        raise ValueError("review context path and pinned hash must be supplied together")
    context = raw
    if native:
        if review_context_path is None or review_context_sha256 is None:
            raise ValueError("native modern plan requires frozen review context evidence")
        context = json.loads(read_pinned(review_context_path, review_context_sha256))
    elif review_context_path is not None:
        raise ValueError("review context sidecar is only for native modern plans")
    plan = EffectiveModernPlan.model_validate(context)
    if native:
        manifest = _native_manifest(raw)
        risk = plan.strategy.risk_policy.model_dump(mode="json")
        if (
            plan.native_plan_sha256 != expected_sha256
            or raw.get("trade_date") != plan.trade_date.isoformat()
            or manifest["strategy_version"] != plan.strategy.strategy_version
            or manifest["effective_config"] != plan.strategy.parameters
            or raw.get("strategy_version") not in {"modern-h15.v1", manifest["strategy_version"]}
            or raw.get("paper_only") is not True
            or any(raw.get(key) != value for key, value in risk.items())
        ):
            raise ValueError("native final plan and frozen review context mismatch")
        plan = plan.model_copy(update={"authorization_strategy_version": raw["strategy_version"]})
    if plan.trade_date != trade_date or max(
        plan.selection_cutoff_utc, plan.effective_at_utc, plan.available_at_utc
    ) > as_of:
        raise ValueError("effective plan date/as_of mismatch")
    return plan


def export_native_context(
    *, plan_path: Path, plan_sha256: str,
    confirmation_path: Path, confirmation_sha256: str,
    first_pool_path: Path, first_pool_sha256: str, active_policy_hash: str,
    selection_cutoff_utc: datetime, as_of: datetime,
) -> EffectiveModernPlan:
    """Export existing context from pinned native facts; no discovery or defaults."""
    from operations.autonomous_selection_handoff import load_open_confirmation

    require_utc(as_of)
    require_utc(selection_cutoff_utc)
    native = json.loads(read_pinned(plan_path, plan_sha256))
    if native.get("schema_version") != "modern_h15_paper_plan.v1":
        raise ValueError("native modern plan required")
    manifest = _native_manifest(native)
    confirmation_raw = read_pinned(confirmation_path, confirmation_sha256)
    receipt = json.loads(confirmation_raw)
    # Never follow an arbitrary path from a sidecar (including a secret file).
    if Path(receipt["config_path"]).resolve() != plan_path.resolve():
        raise ValueError("confirmation must reference the explicit native plan path")
    confirmation = load_open_confirmation(confirmation_path)
    read_pinned(confirmation_path, confirmation_sha256)
    read_pinned(plan_path, plan_sha256)
    auth = confirmation.authorization
    available = require_utc(confirmation.generated_at_utc)
    pool = json.loads(read_pinned(first_pool_path, first_pool_sha256))
    if (
        pool.get("schema_version") != "modern_funnel.first_wave.v2"
        or pool.get("trade_date") != native.get("trade_date")
        or auth.trade_date.isoformat() != native.get("trade_date")
        or auth.config_sha256 != plan_sha256
        or auth.strategy_version != native.get("strategy_version")
        or auth.strategy_version not in {"modern-h15.v1", manifest["strategy_version"]}
        or native.get("paper_only") is not True
        or pool.get("strategy_context", {}).get("active_policy_hash") != active_policy_hash
        or not isinstance(pool.get("candidates"), list)
    ):
        raise ValueError("native confirmation/first pool/active policy mismatch")
    final_symbols = [row["symbol"] for row in native["candidates"]]
    pool_symbols = [row["symbol"] for row in pool["candidates"]]
    if (tuple(final_symbols) != auth.candidate_pool
            or not set(final_symbols).issubset(pool_symbols)):
        raise ValueError("native authorized candidates mismatch complete first pool")
    pool_available = require_utc(datetime.fromisoformat(pool["generated_at_utc"]))
    if (pool_available.astimezone(ZoneInfo("America/New_York")).date() != auth.trade_date
            or available.astimezone(ZoneInfo("America/New_York")).date() != auth.trade_date
            or max(available, selection_cutoff_utc) > as_of):
        raise ValueError("native context date/as_of mismatch")
    effective = available
    if "entry_after_et" in native:
        effective = max(available, datetime.fromisoformat(
            f"{auth.trade_date}T{native['entry_after_et']}"
        ).replace(tzinfo=ZoneInfo("America/New_York")).astimezone(UTC))
    if effective > as_of:
        raise ValueError("native effective time follows as_of")
    strategy = ModernStrategy(
        strategy_id="modern-h15", strategy_version=manifest["strategy_version"],
        active_policy_hash=active_policy_hash, parameters=manifest["effective_config"],
        risk_policy=ModernRiskPolicy.model_validate({
            key: native[key] for key in ModernRiskPolicy.model_fields
        }),
    )
    return EffectiveModernPlan(
        schema_version="loop_effective_modern_plan.v1", trade_date=auth.trade_date,
        strategy=strategy, strategy_sha256=canonical_sha256(strategy.model_dump(mode="json")),
        native_plan_sha256=plan_sha256, authorization_strategy_version=auth.strategy_version,
        effective_at_utc=effective, available_at_utc=available,
        selection_cutoff_utc=selection_cutoff_utc,
        candidate_pool_available_at_utc=pool_available,
        source_snapshot_ids=(
            f"native-sha256:{plan_sha256}", f"confirmation-sha256:{confirmation_sha256}",
            auth.open_confirmation_id, auth.selection_snapshot_id,
            f"complete-first-pool-sha256:{first_pool_sha256}",
        ),
        candidate_pool_complete=True, candidate_pool_source="complete_frozen_morning_pool",
        candidates=tuple(FrozenCandidate.model_validate(row) for row in pool["candidates"]),
    )


def unavailable_execution() -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": "confirmed_fill_evidence_not_supplied",
        "realized_gross_pnl": None,
        "realized_net_pnl": None,
        "fees": None,
    }


def _fill_performance(fills: tuple[BrokerFill, ...], *, costs_complete: bool) -> dict[str, Any]:
    """FIFO of individual execution events; same-time events retain source order."""
    lots: dict[str, deque[tuple[BrokerFill, Decimal]]] = defaultdict(deque)
    gross = matched = realized_fees = Decimal(0)
    costs_known = costs_complete and all(item.fees is not None for item in fills)
    for fill in sorted(fills, key=lambda item: item.filled_at_utc):
        if fill.side == "buy":
            lots[fill.symbol].append((fill, fill.quantity))
            continue
        remaining = fill.quantity
        while remaining:
            if not lots[fill.symbol]:
                raise ValueError("sell fill exceeds confirmed long inventory")
            entry, available = lots[fill.symbol].popleft()
            quantity = min(remaining, available)
            gross += quantity * (fill.price - entry.price)
            matched += quantity
            if costs_known:
                assert entry.fees is not None and fill.fees is not None
                realized_fees += (
                    entry.fees * quantity / entry.quantity + fill.fees * quantity / fill.quantity
                )
            remaining -= quantity
            if quantity < available:
                lots[fill.symbol].appendleft((entry, available - quantity))
    open_quantity = sum((qty for queue in lots.values() for _, qty in queue), Decimal(0))
    return {
        "status": "partial_fills" if open_quantity else ("filled" if fills else "no_trade"),
        "fill_count": len(fills),
        "matched_quantity": float(matched),
        "open_quantity": float(open_quantity),
        "realized_gross_pnl": float(gross) if matched else None,
        "realized_net_pnl": float(gross - realized_fees) if matched and costs_known else None,
        "realized_fees": float(realized_fees) if matched and costs_known else None,
        "fees": float(sum(item.fees for item in fills if item.fees is not None))
        if fills and costs_known else None,
        "cost_status": "available" if fills and costs_known else "unavailable",
        "unrealized_pnl": None,
    }


def build_factual_execution_summary(
    *, plan_path: Path, plan_sha256: str, trade_date: date, as_of: datetime,
    fills_path: Path | None = None, fills_sha256: str | None = None,
    review_context_path: Path | None = None, review_context_sha256: str | None = None,
) -> dict[str, Any]:
    plan = load_effective_plan(
        plan_path, expected_sha256=plan_sha256, trade_date=trade_date, as_of=as_of,
        review_context_path=review_context_path, review_context_sha256=review_context_sha256,
    )
    summary = {
        **unavailable_execution(),
        "schema_version": "loop_factual_execution.v1",
        "orders_authorized": False,
        "trade_date": plan.trade_date.isoformat(),
        "strategy_sha256": plan.strategy_sha256,
        "plan_sha256": plan_sha256,
        "review_context_sha256": review_context_sha256,
        "performance_kind": "factual_broker_execution",
    }
    if (fills_path is None) != (fills_sha256 is None):
        raise ValueError("fill evidence path and pinned hash must be supplied together")
    if fills_path is None or fills_sha256 is None:
        return summary
    raw_evidence = read_pinned(fills_path, fills_sha256)
    evidence = BrokerFillEvidence.model_validate_json(raw_evidence)
    if (
        evidence.trade_date != trade_date or evidence.plan_sha256 != plan_sha256
        or evidence.strategy_sha256 != plan.strategy_sha256
        or evidence.coverage_end_utc > as_of
    ):
        raise ValueError("broker evidence plan/strategy/date/as_of mismatch")
    if any(
        fill.filled_at_utc < max(plan.available_at_utc, plan.effective_at_utc)
        for fill in evidence.fills
    ):
        raise ValueError("broker fill precedes effective plan availability")
    symbols = {fill.symbol for fill in evidence.fills}
    if plan.candidates is not None:
        pool_symbols = {item.symbol for item in plan.candidates}
        if symbols - pool_symbols:
            raise ValueError("broker fill symbol outside complete frozen candidate pool")
        symbols = pool_symbols
    summary.update(
        fill_evidence_sha256=fills_sha256,
        broker_evidence=json.loads(raw_evidence),
        fill_count=len(evidence.fills),
        reason="incomplete_broker_reconciliation",
    )
    if evidence.reconciled_complete and not evidence.fills:
        summary.update(status="no_trade", reason="confirmed_empty_broker_ledger")
    if evidence.reconciled_complete:
        summary.update(_fill_performance(evidence.fills, costs_complete=evidence.costs_complete))
        if evidence.fills:
            summary["reason"] = "confirmed_broker_fills"
        summary["instruments"] = {
            symbol: _fill_performance(
                tuple(item for item in evidence.fills if item.symbol == symbol),
                costs_complete=evidence.costs_complete,
            )
            for symbol in sorted(symbols)
        }
    return summary
