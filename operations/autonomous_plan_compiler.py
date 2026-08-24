"""Freeze one current, fully observed selection into a bounded Paper plan."""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import polars as pl

from data_plane.contracts import DatasetSnapshot
from operations.autonomous_paper_config import SCHEMA_VERSION

POLL_SECONDS = 1
HARD_STOP_FRACTION = Decimal("0.02")
MAX_NOTIONAL_FRACTION = Decimal("0.10")
FULL_RISK_FRACTION = Decimal("0.0035")
MAX_SPREAD_RATIO = Decimal("0.0025")


@dataclass(frozen=True)
class PreparedAutonomousPaperPlan:
    """Secret-free receipt for one immutable selection-to-plan conversion."""

    trade_date: date
    symbol: str
    plan_id: str
    selection_snapshot_id: str
    output_path: Path


def compile_autonomous_paper_plans(
    *,
    data_root: Path,
    trade_date: date,
    output_path: Path,
    max_plans: int = 5,
) -> tuple[PreparedAutonomousPaperPlan, ...]:
    """Freeze the highest-ranked eligible selections into one Paper config."""

    if max_plans < 1:
        raise ValueError("max_plans must be positive")
    snapshot, frame = _current_selection(data_root=data_root, trade_date=trade_date)
    rows = _eligible_candidates(frame)[:max_plans]
    if not rows:
        raise ValueError("no eligible current selection candidate")
    prepared: list[PreparedAutonomousPaperPlan] = []
    plans: list[dict[str, object]] = []
    for row in rows:
        symbol = _symbol(row)
        reference = _price(row, "premarket_close")
        _require_candidate_gates(row)
        gate_asof = _utc_timestamp(row, "gate_asof_utc")
        catalyst_score = _finite_number(row.get("earnings_intensity_score"))
        sector_symbol = _optional_symbol(row.get("sector_symbol")) or "N/A"
        hard_stop = (reference * (Decimal(1) - HARD_STOP_FRACTION)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if hard_stop <= 0 or hard_stop >= reference:
            continue
        plan_id = f"auto-{trade_date:%Y%m%d}-{symbol}"
        plans.append(
            _plan_payload(
                trade_date=trade_date,
                symbol=symbol,
                plan_id=plan_id,
                reference=reference,
                hard_stop=hard_stop,
                selection_snapshot_id=snapshot.dataset_id,
                gate_asof=gate_asof,
                catalyst_score=catalyst_score,
                sector_symbol=sector_symbol,
                safety_envelope=f"safety/{plan_id}.json",
            )
        )
        prepared.append(
            PreparedAutonomousPaperPlan(
                trade_date=trade_date,
                symbol=symbol,
                plan_id=plan_id,
                selection_snapshot_id=snapshot.dataset_id,
                output_path=output_path,
            )
        )
    if not plans:
        raise ValueError("no eligible current selection candidate")
    _write_atomic_json(
        output_path,
        {"schema_version": SCHEMA_VERSION, "poll_seconds": POLL_SECONDS, "plans": plans},
    )
    return tuple(prepared)


def compile_autonomous_paper_plan(
    *,
    data_root: Path,
    trade_date: date,
    output_path: Path,
) -> PreparedAutonomousPaperPlan:
    """Create a single constrained plan or fail without guessing missing facts."""

    snapshot, frame = _current_selection(data_root=data_root, trade_date=trade_date)
    row = _top_candidate(frame)
    symbol = _symbol(row)
    reference = _price(row, "premarket_close")
    _require_candidate_gates(row)
    gate_asof = _utc_timestamp(row, "gate_asof_utc")
    catalyst_score = _finite_number(row.get("earnings_intensity_score"))
    sector_symbol = _optional_symbol(row.get("sector_symbol")) or "N/A"
    hard_stop = (reference * (Decimal(1) - HARD_STOP_FRACTION)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    if hard_stop <= 0 or hard_stop >= reference:
        raise ValueError("no eligible current selection candidate")
    plan_id = f"auto-{trade_date:%Y%m%d}-{symbol}"
    payload = _payload(
        trade_date=trade_date,
        symbol=symbol,
        plan_id=plan_id,
        reference=reference,
        hard_stop=hard_stop,
        selection_snapshot_id=snapshot.dataset_id,
        gate_asof=gate_asof,
        catalyst_score=catalyst_score,
        sector_symbol=sector_symbol,
    )
    _write_atomic_json(output_path, payload)
    return PreparedAutonomousPaperPlan(
        trade_date=trade_date,
        symbol=symbol,
        plan_id=plan_id,
        selection_snapshot_id=snapshot.dataset_id,
        output_path=output_path,
    )


def _current_selection(
    *,
    data_root: Path,
    trade_date: date,
) -> tuple[DatasetSnapshot, pl.DataFrame]:
    matches: list[tuple[DatasetSnapshot, pl.DataFrame]] = []
    for parquet_path in (data_root / "accepted").glob(
        "kernel.universe.selection_gates-*/data.parquet"
    ):
        try:
            snapshot = DatasetSnapshot.model_validate_json(
                (parquet_path.parent / "manifest.json").read_text(encoding="utf-8")
            ).assert_usable()
            if snapshot.schema_version != "selection_gates.v2":
                continue
            frame = pl.read_parquet(parquet_path)
            dates = frame.get_column("session_date").drop_nulls().unique().to_list()
            if dates == [trade_date]:
                matches.append((snapshot, frame))
        except (
            OSError,
            ValueError,
            pl.exceptions.PolarsError,
        ):
            continue
    if not matches:
        raise FileNotFoundError("current accepted selection is required")
    return max(matches, key=lambda item: item[0].asof_utc)


def _top_candidate(frame: pl.DataFrame) -> dict[str, Any]:
    survivors = _eligible_candidates(frame)
    if not survivors:
        raise ValueError("no eligible current selection candidate")
    return survivors[0]


def _eligible_candidates(frame: pl.DataFrame) -> list[dict[str, Any]]:
    required = {
        "symbol",
        "selection_rank",
        "pass_gate",
        "rvol",
        "price",
        "premarket_close",
        "premarket_above_vwap",
        "directional_volume_confirmed",
        "gate_asof_utc",
    }
    if required - set(frame.columns):
        raise ValueError("no eligible current selection candidate")
    survivors = frame.filter(pl.col("pass_gate")).sort(
        "selection_rank",
        "symbol",
    )
    if survivors.is_empty() or survivors.height != survivors.get_column("symbol").n_unique():
        raise ValueError("no eligible current selection candidate")
    return [
        {str(key): value for key, value in row.items()}
        for row in survivors.iter_rows(named=True)
    ]


def _require_candidate_gates(row: dict[str, Any]) -> None:
    rank = row.get("selection_rank")
    rvol = _finite_number(row.get("rvol"))
    price = _finite_number(row.get("price"))
    intensity = _finite_number(row.get("earnings_intensity_score"))
    if (
        isinstance(rank, bool)
        or not isinstance(rank, int)
        or rank <= 0
        or rvol is None
        or rvol <= 3.0
        or price is None
        or price <= 0
        or (intensity is not None and not 0 <= intensity <= 100)
        or row.get("premarket_above_vwap") is not True
        or row.get("directional_volume_confirmed") is not True
    ):
        raise ValueError("no eligible current selection candidate")


def _symbol(row: dict[str, Any]) -> str:
    value = row.get("symbol")
    if not isinstance(value, str):
        raise ValueError("no eligible current selection candidate")
    normalized = value.strip().upper()
    if not normalized or normalized != value or len(normalized) > 16:
        raise ValueError("no eligible current selection candidate")
    return normalized


def _optional_symbol(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("no eligible current selection candidate")
    normalized = value.strip().upper()
    if not normalized or len(normalized) > 16:
        raise ValueError("no eligible current selection candidate")
    return normalized


def _price(row: dict[str, Any], name: str) -> Decimal:
    value = _finite_number(row.get(name))
    if value is None or value <= 0:
        raise ValueError("no eligible current selection candidate")
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _utc_timestamp(row: dict[str, Any], name: str) -> datetime:
    value = row.get(name)
    if not isinstance(value, datetime):
        raise ValueError("no eligible current selection candidate")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("no eligible current selection candidate")
    return value


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _payload(
    *,
    trade_date: date,
    symbol: str,
    plan_id: str,
    reference: Decimal,
    hard_stop: Decimal,
    selection_snapshot_id: str,
    gate_asof: datetime,
    catalyst_score: float | None,
    sector_symbol: str,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "poll_seconds": POLL_SECONDS,
        "plans": [
            _plan_payload(
                trade_date=trade_date,
                symbol=symbol,
                plan_id=plan_id,
                reference=reference,
                hard_stop=hard_stop,
                selection_snapshot_id=selection_snapshot_id,
                gate_asof=gate_asof,
                catalyst_score=catalyst_score,
                sector_symbol=sector_symbol,
                safety_envelope=f"../safety/{plan_id}.json",
            )
        ],
    }


def _plan_payload(
    *,
    trade_date: date,
    symbol: str,
    plan_id: str,
    reference: Decimal,
    hard_stop: Decimal,
    selection_snapshot_id: str,
    gate_asof: datetime,
    catalyst_score: float | None,
    sector_symbol: str,
    safety_envelope: str,
) -> dict[str, object]:
    provenance = (
        "operations.autonomous_plan_compiler.v1|"
        f"selection={selection_snapshot_id}|"
        "entry=selection_top_rank|reference=premarket_close|"
        "hard_stop=reference_minus_2pct"
    )
    return {
        "plan": {
            "plan_id": plan_id,
            "symbol": symbol,
            "trade_date": trade_date.isoformat(),
            "reference_price": f"{reference:.2f}",
            "hard_stop": f"{hard_stop:.2f}",
            "max_notional_fraction": f"{MAX_NOTIONAL_FRACTION:.2f}",
            "full_risk_fraction": f"{FULL_RISK_FRACTION:.4f}",
            "max_spread_ratio": f"{MAX_SPREAD_RATIO:.4f}",
            "source_snapshot_ids": [selection_snapshot_id],
            "provenance": provenance,
        },
        "policy_evidence": {
            "route": "catalyst",
            "catalyst": {
                "value": catalyst_score,
                "asof_utc": gate_asof.isoformat(),
                "provenance": (
                    f"{selection_snapshot_id}|earnings_intensity_score"
                    if catalyst_score is not None
                    else f"{selection_snapshot_id}|catalyst_score_unavailable"
                ),
            },
            "factor": {
                "value": None,
                "asof_utc": gate_asof.isoformat(),
                "provenance": "factor_route_unavailable",
            },
            "right_tail": {
                "value": None,
                "asof_utc": gate_asof.isoformat(),
                "provenance": "right_tail_unavailable",
            },
            "first_target_reward_r": 2.5,
            "weighted_expected_reward_r": 3.0,
            "reward_risk_provenance": (
                "operations.autonomous_plan_compiler.v1|"
                "fixed_reward_risk_floor=2.5R/3.0R"
            ),
            "a_plus_plus_approved": False,
        },
        "market_context": {
            "benchmark_symbol": "SPY",
            "sector_symbol": sector_symbol,
            "provenance": (
                f"{selection_snapshot_id}|sector_symbol"
                if sector_symbol != "N/A"
                else f"{selection_snapshot_id}|sector_symbol_unavailable"
            ),
        },
        "safety_envelope": safety_envelope,
    }


def _write_atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
