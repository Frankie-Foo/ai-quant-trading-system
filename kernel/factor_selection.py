"""Point-in-time pure-factor candidate selection, independent of catalyst evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime

import polars as pl


@dataclass(frozen=True)
class FactorSelectionPolicy:
    """Transparent research policy; thresholds are not production-approved alpha."""

    min_rvol: float = 3.0
    min_gap_return: float = 0.0
    min_score: float = 60.0
    max_candidates: int = 50
    rvol_full_score: float = 8.0
    gap_full_score: float = 0.08
    premarket_return_full_score: float = 0.08
    vwap_extension_full_score: float = 0.03
    beta_full_score: float = 3.0
    atr_full_score: float = 0.08

    def __post_init__(self) -> None:
        positive = (
            self.min_rvol,
            self.rvol_full_score,
            self.gap_full_score,
            self.premarket_return_full_score,
            self.vwap_extension_full_score,
            self.beta_full_score,
            self.atr_full_score,
        )
        if any(not math.isfinite(value) or value <= 0 for value in positive):
            raise ValueError("factor policy scale values must be finite and positive")
        if (
            not math.isfinite(self.min_gap_return)
            or not math.isfinite(self.min_score)
            or not 0 <= self.min_score <= 100
            or self.max_candidates <= 0
        ):
            raise ValueError("factor policy thresholds are invalid")


DAILY_COLUMNS = {
    "symbol",
    "precheck_pass",
    "reject_reason",
    "price",
    "adv_usd",
    "beta",
    "atr_pct",
    "price_provenance",
    "adv_usd_provenance",
    "beta_provenance",
    "atr_pct_provenance",
}
PREMARKET_COLUMNS = {
    "symbol",
    "session_date",
    "availability",
    "rvol",
    "premarket_return",
    "premarket_close",
    "premarket_vwap",
    "premarket_close_location",
    "premarket_price_confirmation",
    "data_cutoff_utc",
    "rvol_provenance",
    "premarket_price_provenance",
}


def _finite(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _scaled(value: float | None, full_score_value: float, weight: float) -> float:
    if value is None:
        return 0.0
    return weight * min(max(value / full_score_value, 0.0), 1.0)


def _require_frame(
    frame: pl.DataFrame, *, name: str, columns: set[str]
) -> None:
    missing = columns - set(frame.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {sorted(missing)}")
    if frame.get_column("symbol").n_unique() != frame.height:
        raise ValueError(f"{name} contains duplicate symbols")


def select_factor_candidates(
    daily_universe: pl.DataFrame,
    premarket_features: pl.DataFrame,
    *,
    trade_date: date,
    asof_utc: datetime,
    policy: FactorSelectionPolicy,
) -> pl.DataFrame:
    """Score every daily precheck row without consulting catalyst membership.

    The returned frame is the module interface and the test surface. It includes
    rejected rows so missing evidence and rank cutoffs remain auditable.
    """

    if asof_utc.tzinfo is None or asof_utc.utcoffset() != UTC.utcoffset(asof_utc):
        raise ValueError("asof_utc must be timezone-aware UTC")
    _require_frame(daily_universe, name="daily_universe", columns=DAILY_COLUMNS)
    _require_frame(
        premarket_features,
        name="premarket_features",
        columns=PREMARKET_COLUMNS,
    )
    wrong_session = premarket_features.filter(pl.col("session_date") != trade_date)
    if wrong_session.height:
        raise ValueError("premarket features must use the target trade date")
    future = premarket_features.filter(pl.col("data_cutoff_utc") > asof_utc)
    if future.height:
        raise ValueError("premarket features contain data after asof")

    premarket_by_symbol = {
        str(row["symbol"]): row
        for row in premarket_features.iter_rows(named=True)
    }
    output: list[dict[str, object]] = []
    for daily in daily_universe.sort("symbol").iter_rows(named=True):
        symbol = str(daily["symbol"])
        premarket = premarket_by_symbol.get(symbol)
        reasons: list[str] = []
        if not bool(daily["precheck_pass"]):
            reasons.append(
                f"daily_precheck:{str(daily.get('reject_reason') or 'failed')}"
            )
        if premarket is None:
            reasons.append("missing_premarket_features")

        price = _finite(daily.get("price"))
        beta = _finite(daily.get("beta"))
        atr_pct = _finite(daily.get("atr_pct"))
        rvol = None if premarket is None else _finite(premarket.get("rvol"))
        premarket_return = (
            None if premarket is None else _finite(premarket.get("premarket_return"))
        )
        premarket_close = (
            None if premarket is None else _finite(premarket.get("premarket_close"))
        )
        premarket_vwap = (
            None if premarket is None else _finite(premarket.get("premarket_vwap"))
        )
        close_location = (
            None
            if premarket is None
            else _finite(premarket.get("premarket_close_location"))
        )
        gap_return = (
            premarket_close / price - 1
            if premarket_close is not None and price is not None and price > 0
            else None
        )
        vwap_extension = (
            premarket_close / premarket_vwap - 1
            if premarket_close is not None
            and premarket_vwap is not None
            and premarket_vwap > 0
            else None
        )

        if premarket is not None:
            if premarket.get("availability") != "available" or rvol is None:
                reasons.append("rvol_unavailable")
            elif rvol <= policy.min_rvol:
                reasons.append("rvol_below_min")
            if not bool(premarket.get("premarket_price_confirmation")):
                reasons.append("premarket_price_not_confirmed")
            if gap_return is None or gap_return <= policy.min_gap_return:
                reasons.append("premarket_gap_not_positive")

        rvol_score = _scaled(rvol, policy.rvol_full_score, 30.0)
        gap_score = _scaled(gap_return, policy.gap_full_score, 20.0)
        premarket_return_score = _scaled(
            premarket_return,
            policy.premarket_return_full_score,
            15.0,
        )
        close_location_score = _scaled(close_location, 1.0, 10.0)
        vwap_score = _scaled(
            vwap_extension,
            policy.vwap_extension_full_score,
            10.0,
        )
        beta_score = _scaled(beta, policy.beta_full_score, 7.5)
        atr_score = _scaled(atr_pct, policy.atr_full_score, 7.5)
        factor_score = round(
            rvol_score
            + gap_score
            + premarket_return_score
            + close_location_score
            + vwap_score
            + beta_score
            + atr_score,
            6,
        )
        if not reasons and factor_score < policy.min_score:
            reasons.append("factor_score_below_min")

        row = dict(daily)
        if premarket is not None:
            row.update(premarket)
        row.update(
            {
                "candidate_source": "factor",
                "factor_score": factor_score,
                "factor_rvol_score": round(rvol_score, 6),
                "factor_gap_score": round(gap_score, 6),
                "factor_premarket_return_score": round(
                    premarket_return_score, 6
                ),
                "factor_close_location_score": round(
                    close_location_score, 6
                ),
                "factor_vwap_score": round(vwap_score, 6),
                "factor_beta_score": round(beta_score, 6),
                "factor_atr_score": round(atr_score, 6),
                "premarket_gap_return": gap_return,
                "premarket_vwap_extension": vwap_extension,
                "factor_pass": not reasons,
                "factor_reject_reason": ";".join(reasons),
                "factor_rank": None,
                "factor_asof_utc": asof_utc,
                "factor_provenance": (
                    "kernel.factor_selection.v1|"
                    f"daily={daily.get('price_provenance')}|"
                    f"premarket={None if premarket is None else premarket.get('rvol_provenance')}"
                ),
                "production_eligible": False,
            }
        )
        output.append(row)

    if not output:
        return pl.DataFrame()
    result = pl.DataFrame(output)
    eligible = (
        result.filter(pl.col("factor_pass"))
        .sort(
            ["factor_score", "rvol", "symbol"],
            descending=[True, True, False],
            nulls_last=True,
        )
        .get_column("symbol")
        .to_list()
    )
    selected = eligible[: policy.max_candidates]
    selected_ranks = {
        str(symbol): rank for rank, symbol in enumerate(selected, start=1)
    }
    return (
        result.with_columns(
            pl.col("symbol")
            .replace_strict(
                selected_ranks,
                default=None,
                return_dtype=pl.Int64,
            )
            .alias("factor_rank")
        )
        .with_columns(
            (pl.col("factor_rank").is_not_null()).alias("factor_pass"),
            pl.when(
                pl.col("factor_rank").is_null()
                & (pl.col("factor_reject_reason") == "")
            )
            .then(pl.lit("outside_factor_top_n"))
            .otherwise(pl.col("factor_reject_reason"))
            .alias("factor_reject_reason"),
        )
        .sort("symbol")
    )
