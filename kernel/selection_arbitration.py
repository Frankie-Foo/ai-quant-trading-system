"""Deterministic shadow arbitration across catalyst, factor and order-flow evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

import polars as pl

CATALYST_COLUMNS = {
    "symbol",
    "pass_gate",
    "selection_rank",
    "gate_asof_utc",
}
FACTOR_COLUMNS = {
    "symbol",
    "factor_pass",
    "factor_rank",
    "factor_score",
    "factor_asof_utc",
}
ORDER_FLOW_COLUMNS = {
    "symbol",
    "availability",
    "order_flow_confirmation_score",
    "data_cutoff_utc",
    "order_flow_provenance",
}


@dataclass(frozen=True)
class ShadowArbitrationPolicy:
    """Versionable score policy for research comparison, never direct execution."""

    intersection_bonus: float = 5.0
    order_flow_weight: float = 0.3
    max_order_flow_adjustment: float = 15.0
    max_candidates: int = 50

    def __post_init__(self) -> None:
        values = (
            self.intersection_bonus,
            self.order_flow_weight,
            self.max_order_flow_adjustment,
        )
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("arbitration score policy must be finite and non-negative")
        if self.max_candidates <= 0:
            raise ValueError("max_candidates must be positive")


def _require(
    frame: pl.DataFrame,
    *,
    name: str,
    columns: set[str],
    cutoff_column: str,
    asof_utc: datetime,
) -> None:
    missing = columns - set(frame.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {sorted(missing)}")
    if frame.get_column("symbol").n_unique() != frame.height:
        raise ValueError(f"{name} contains duplicate symbols")
    timestamp_type = frame.schema[cutoff_column]
    if (
        not isinstance(timestamp_type, pl.Datetime)
        or timestamp_type.time_zone != "UTC"
    ):
        raise ValueError(f"{name} cutoff timestamps must be timezone-aware UTC")
    if frame.filter(pl.col(cutoff_column) > asof_utc).height:
        raise ValueError(f"{name} contains data after asof")


def _number(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _empty() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "symbol": pl.String,
            "candidate_source": pl.String,
            "catalyst_rank": pl.Int64,
            "catalyst_score": pl.Float64,
            "factor_rank": pl.Int64,
            "factor_score": pl.Float64,
            "intersection_bonus": pl.Float64,
            "order_flow_availability": pl.String,
            "order_flow_confirmation_score": pl.Float64,
            "order_flow_adjustment": pl.Float64,
            "unified_score": pl.Float64,
            "unified_rank": pl.Int64,
            "arbitration_asof_utc": pl.Datetime("ms", "UTC"),
            "arbitration_provenance": pl.String,
            "production_eligible": pl.Boolean,
            "execution_eligible": pl.Boolean,
        }
    )


def arbitrate_shadow_candidates(
    catalyst_candidates: pl.DataFrame,
    factor_candidates: pl.DataFrame,
    order_flow: pl.DataFrame,
    *,
    asof_utc: datetime,
    policy: ShadowArbitrationPolicy,
) -> pl.DataFrame:
    """Unify independent candidate generators; order flow confirms but cannot create."""

    if asof_utc.tzinfo is None or asof_utc.utcoffset() != UTC.utcoffset(asof_utc):
        raise ValueError("asof_utc must be timezone-aware UTC")
    _require(
        catalyst_candidates,
        name="catalyst_candidates",
        columns=CATALYST_COLUMNS,
        cutoff_column="gate_asof_utc",
        asof_utc=asof_utc,
    )
    _require(
        factor_candidates,
        name="factor_candidates",
        columns=FACTOR_COLUMNS,
        cutoff_column="factor_asof_utc",
        asof_utc=asof_utc,
    )
    _require(
        order_flow,
        name="order_flow",
        columns=ORDER_FLOW_COLUMNS,
        cutoff_column="data_cutoff_utc",
        asof_utc=asof_utc,
    )

    catalyst_rows = catalyst_candidates.filter(pl.col("pass_gate"))
    factor_rows = factor_candidates.filter(pl.col("factor_pass"))
    if catalyst_rows.filter(pl.col("selection_rank").is_null()).height:
        raise ValueError("passing catalyst candidates require selection_rank")
    if factor_rows.filter(
        pl.col("factor_rank").is_null() | pl.col("factor_score").is_null()
    ).height:
        raise ValueError("passing factor candidates require rank and score")

    catalyst_rank_values = catalyst_rows.get_column("selection_rank").to_list()
    if any(not isinstance(value, int) for value in catalyst_rank_values):
        raise ValueError("passing catalyst candidate ranks must be integers")
    catalyst_max_rank = max(catalyst_rank_values, default=0)
    catalyst_by_symbol = {
        str(row["symbol"]): row
        for row in catalyst_rows.iter_rows(named=True)
    }
    factor_by_symbol = {
        str(row["symbol"]): row for row in factor_rows.iter_rows(named=True)
    }
    flow_by_symbol = {
        str(row["symbol"]): row for row in order_flow.iter_rows(named=True)
    }
    symbols = sorted(set(catalyst_by_symbol) | set(factor_by_symbol))
    if not symbols:
        return _empty()

    output: list[dict[str, object]] = []
    for symbol in symbols:
        catalyst = catalyst_by_symbol.get(symbol)
        factor = factor_by_symbol.get(symbol)
        flow = flow_by_symbol.get(symbol)
        catalyst_rank = (
            None if catalyst is None else int(catalyst["selection_rank"])
        )
        catalyst_score = (
            None
            if catalyst_rank is None or catalyst_max_rank <= 0
            else 100.0
            * (catalyst_max_rank - catalyst_rank + 1)
            / catalyst_max_rank
        )
        factor_rank = None if factor is None else int(factor["factor_rank"])
        factor_score = (
            None if factor is None else _number(factor.get("factor_score"))
        )
        both = catalyst is not None and factor is not None
        source = "catalyst+factor" if both else ("catalyst" if catalyst else "factor")
        base_score = max(catalyst_score or 0.0, factor_score or 0.0)
        bonus = policy.intersection_bonus if both else 0.0

        flow_availability = (
            "not_requested" if flow is None else str(flow.get("availability"))
        )
        flow_score = (
            None
            if flow is None or flow_availability != "available"
            else _number(flow.get("order_flow_confirmation_score"))
        )
        raw_adjustment = (
            0.0
            if flow_score is None
            else (flow_score - 50.0) * policy.order_flow_weight
        )
        flow_adjustment = min(
            max(raw_adjustment, -policy.max_order_flow_adjustment),
            policy.max_order_flow_adjustment,
        )
        output.append(
            {
                "symbol": symbol,
                "candidate_source": source,
                "catalyst_rank": catalyst_rank,
                "catalyst_score": catalyst_score,
                "factor_rank": factor_rank,
                "factor_score": factor_score,
                "intersection_bonus": bonus,
                "order_flow_availability": flow_availability,
                "order_flow_confirmation_score": flow_score,
                "order_flow_adjustment": flow_adjustment,
                "unified_score": round(base_score + bonus + flow_adjustment, 6),
                "unified_rank": None,
                "arbitration_asof_utc": asof_utc,
                "arbitration_provenance": (
                    "kernel.selection_arbitration.v1|"
                    f"intersection_bonus={policy.intersection_bonus}|"
                    f"order_flow_weight={policy.order_flow_weight}|"
                    f"max_order_flow_adjustment={policy.max_order_flow_adjustment}|"
                    f"flow={None if flow is None else flow.get('order_flow_provenance')}"
                ),
                "production_eligible": False,
                "execution_eligible": False,
            }
        )

    result = pl.DataFrame(output)
    selected = (
        result.sort(
            ["unified_score", "factor_score", "catalyst_score", "symbol"],
            descending=[True, True, True, False],
            nulls_last=True,
        )
        .head(policy.max_candidates)
        .get_column("symbol")
        .to_list()
    )
    rank_by_symbol = {
        str(symbol): rank for rank, symbol in enumerate(selected, start=1)
    }
    return (
        result.filter(pl.col("symbol").is_in(selected))
        .with_columns(
            pl.col("symbol")
            .replace_strict(
                rank_by_symbol,
                default=None,
                return_dtype=pl.Int64,
            )
            .alias("unified_rank")
        )
        .sort("unified_rank")
    )
