"""L0 point-in-time daily universe construction.

This layer computes only fields supported by the accepted daily-bar history.  It
fails closed while RVOL, point-in-time market capitalisation, earnings windows,
and LULD state are unavailable.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

import polars as pl

from kernel.config import Config, load_config

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = ROOT / "data"
ATR_WINDOW = 14
BETA_WINDOW = 252
ADV_WINDOW = 20
LOAD_SESSION_WINDOW = 300
IDENTITY_DISCONTINUITY_RETURN = 0.90

REQUIRED_UNIVERSE_COLUMNS = (
    "symbol",
    "price",
    "adv_usd",
    "market_cap",
    "beta",
    "atr_pct",
    "rvol",
    "tier",
    "pass_gate",
    "reject_reason",
)

PENDING_FIELDS = "pending:rvol,market_cap,earnings,luld"


def _number(value: object) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def market_cap_tier(market_cap: float | None, cfg: Config) -> str | None:
    """Map a finite positive market cap to the frozen sizing tier boundaries."""
    value = _number(market_cap)
    if value is None or value <= 0:
        return None
    if value >= cfg.tiers.mega.min_cap:
        return "mega"
    if value >= cfg.tiers.large.min_cap:
        return "large"
    if value >= cfg.tiers.mid.min_cap:
        return "mid"
    return "small"


def apply_selection_gates(
    daily_universe: pl.DataFrame,
    catalyst_candidates: pl.DataFrame,
    rvol_features: pl.DataFrame,
    market_details: pl.DataFrame,
    earnings_calendar: pl.DataFrame,
    trade_halts: pl.DataFrame,
    free_float: pl.DataFrame,
    *,
    trade_date: date,
    asof_utc: datetime,
    recent_session_dates: Iterable[date],
    cfg: Config,
    low_float_shares: int,
) -> pl.DataFrame:
    """Apply deterministic L0 gates to the locked catalyst pool.

    Later halt records are ignored mechanically. Unavailable RVOL, market cap, or
    low-float evidence fails closed instead of being imputed.
    """
    if asof_utc.tzinfo is None or asof_utc.utcoffset() is None:
        raise ValueError("asof_utc must be timezone-aware")
    if low_float_shares <= 0:
        raise ValueError("low_float_shares must be positive")
    for name, frame, required in (
        ("daily_universe", daily_universe, {"symbol", "precheck_pass", "reject_reason"}),
        ("catalyst_candidates", catalyst_candidates, {"symbol"}),
        ("rvol_features", rvol_features, {"symbol", "rvol", "availability"}),
        ("market_details", market_details, {"symbol", "market_cap"}),
        ("earnings_calendar", earnings_calendar, {"symbol", "trade_date"}),
        (
            "trade_halts",
            trade_halts,
            {"symbol", "halt_date", "halt_ts_utc", "resumption_ts_utc", "is_luld"},
        ),
        ("free_float", free_float, {"symbol", "free_float"}),
    ):
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"{name} missing required columns: {sorted(missing)}")

    if catalyst_candidates.is_empty():
        return catalyst_candidates.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("price"),
            pl.lit(None, dtype=pl.Float64).alias("adv_usd"),
            pl.lit(None, dtype=pl.Float64).alias("beta"),
            pl.lit(None, dtype=pl.Float64).alias("atr_pct"),
            pl.lit(None, dtype=pl.Date).alias("asof_date"),
            pl.lit(None, dtype=pl.String).alias("price_provenance"),
            pl.lit(None, dtype=pl.String).alias("adv_usd_provenance"),
            pl.lit(None, dtype=pl.String).alias("beta_provenance"),
            pl.lit(None, dtype=pl.String).alias("atr_pct_provenance"),
            pl.lit(None, dtype=pl.Float64).alias("market_cap"),
            pl.lit(None, dtype=pl.Date).alias("market_cap_asof_date"),
            pl.lit(None, dtype=pl.String).alias("market_cap_provenance"),
            pl.lit(None, dtype=pl.String).alias("tier"),
            pl.lit(None, dtype=pl.Float64).alias("rvol"),
            pl.lit(None, dtype=pl.String).alias("rvol_availability"),
            pl.lit(None, dtype=pl.String).alias("rvol_provenance"),
            pl.lit(None, dtype=pl.Float64).alias("free_float"),
            pl.lit(None, dtype=pl.Date).alias("free_float_effective_date"),
            pl.lit(None, dtype=pl.String).alias("free_float_provenance"),
            pl.lit(False).alias("earnings_day"),
            pl.lit(False).alias("current_halt"),
            pl.lit(0, dtype=pl.Int64).alias("recent_luld_count"),
            pl.lit(False).alias("luld_risk"),
            pl.lit(False).alias("pass_gate"),
            pl.lit("", dtype=pl.String).alias("reject_reason"),
            pl.lit(asof_utc).cast(pl.Datetime("ms", "UTC")).alias("gate_asof_utc"),
            pl.lit(None, dtype=pl.Int64).alias("selection_rank"),
        )

    locked_symbols = catalyst_candidates.get_column("symbol").to_list()
    if len(locked_symbols) != len(set(locked_symbols)):
        raise ValueError("catalyst candidate symbols must be unique")
    daily_map = {
        str(row["symbol"]): row for row in daily_universe.iter_rows(named=True)
    }
    rvol_map = {
        str(row["symbol"]): row for row in rvol_features.iter_rows(named=True)
    }
    market_map = {
        str(row["symbol"]): row for row in market_details.iter_rows(named=True)
    }
    float_map = {str(row["symbol"]): row for row in free_float.iter_rows(named=True)}
    earnings_symbols = set(
        earnings_calendar.filter(pl.col("trade_date") == trade_date)
        .get_column("symbol")
        .to_list()
    )
    recent_dates = set(recent_session_dates)
    known_halts = trade_halts.filter(pl.col("halt_ts_utc") <= asof_utc)
    halts_by_symbol: dict[str, list[dict[str, object]]] = {}
    for halt in known_halts.iter_rows(named=True):
        halts_by_symbol.setdefault(str(halt["symbol"]), []).append(halt)

    output: list[dict[str, object]] = []
    for catalyst in catalyst_candidates.iter_rows(named=True):
        symbol = str(catalyst["symbol"])
        daily = daily_map.get(symbol, {})
        rvol_row = rvol_map.get(symbol, {})
        market = market_map.get(symbol, {})
        float_row = float_map.get(symbol, {})
        cap = _number(market.get("market_cap"))
        rvol_value = _number(rvol_row.get("rvol"))
        float_value = _number(float_row.get("free_float"))
        symbol_halts = halts_by_symbol.get(symbol, [])
        current_halt = False
        for halt in symbol_halts:
            resumption = halt.get("resumption_ts_utc")
            if not isinstance(resumption, datetime) or resumption > asof_utc:
                current_halt = True
                break
        recent_luld_count = sum(
            halt.get("halt_date") in recent_dates and bool(halt.get("is_luld"))
            for halt in symbol_halts
        )
        luld_risk = recent_luld_count > 0 and (
            float_value is None or float_value < low_float_shares
        )
        earnings_day = symbol in earnings_symbols

        reasons: list[str] = []
        if not bool(daily.get("precheck_pass")):
            daily_reason = str(daily.get("reject_reason") or "missing_daily_precheck")
            reasons.append(f"daily_precheck:{daily_reason}")
        if rvol_row.get("availability") != "available" or rvol_value is None:
            reasons.append("missing_rvol")
        elif rvol_value <= cfg.universe.min_rvol:
            reasons.append("rvol_below_or_equal_min")
        if cap is None or cap <= 0:
            reasons.append("missing_market_cap")
        if earnings_day:
            reasons.append("earnings_day")
        if current_halt:
            reasons.append("current_trading_halt")
        if luld_risk:
            reasons.append("recent_luld_low_or_unknown_float")

        row = dict(catalyst)
        for column in (
            "price",
            "adv_usd",
            "beta",
            "atr_pct",
            "asof_date",
            "price_provenance",
            "adv_usd_provenance",
            "beta_provenance",
            "atr_pct_provenance",
        ):
            if column in daily:
                row[column] = daily[column]
        row.update(
            {
                "market_cap": cap,
                "market_cap_asof_date": market.get("asof_date"),
                "market_cap_provenance": market.get("provenance"),
                "tier": market_cap_tier(cap, cfg),
                "rvol": rvol_value,
                "rvol_availability": rvol_row.get("availability"),
                "rvol_provenance": rvol_row.get("rvol_provenance"),
                "free_float": float_value,
                "free_float_effective_date": float_row.get("effective_date"),
                "free_float_provenance": float_row.get("provenance"),
                "earnings_day": earnings_day,
                "current_halt": current_halt,
                "recent_luld_count": recent_luld_count,
                "luld_risk": luld_risk,
                "pass_gate": not reasons,
                "reject_reason": ";".join(reasons) if reasons else "",
                "gate_asof_utc": asof_utc,
                "selection_rank": None,
            }
        )
        output.append(row)

    result = pl.DataFrame(output).sort("symbol")
    survivors = (
        result.filter(pl.col("pass_gate"))
        .sort("rvol", "symbol", descending=[True, False])
        .get_column("symbol")
        .to_list()
    )
    rank_by_symbol = {symbol: rank for rank, symbol in enumerate(survivors, start=1)}
    return result.with_columns(
        pl.col("symbol")
        .replace_strict(rank_by_symbol, default=None, return_dtype=pl.Int64)
        .alias("selection_rank")
    ).sort("symbol")


def _reason(row: dict[str, object], cfg: Config) -> tuple[bool, str]:
    reasons: list[str] = []
    price = _number(row.get("price"))
    beta_value = _number(row.get("beta"))
    atr_pct = _number(row.get("atr_pct"))
    max_abs_return = _number(row.get("max_abs_return"))

    if price is None:
        reasons.append("missing_price")
    elif price < cfg.universe.min_price:
        reasons.append("price_below_min")

    if max_abs_return is not None and max_abs_return > IDENTITY_DISCONTINUITY_RETURN:
        reasons.append("suspected_identity_discontinuity")

    beta_known = beta_value is not None
    atr_known = atr_pct is not None
    if not beta_known and not atr_known:
        reasons.append("insufficient_elasticity_history")
    elif not (
        (beta_value is not None and beta_value > cfg.universe.min_beta)
        or (atr_pct is not None and atr_pct > cfg.universe.min_atr_pct)
    ):
        reasons.append("low_elasticity")

    precheck_pass = not reasons
    return precheck_pass, PENDING_FIELDS if precheck_pass else ";".join(reasons)


def _build_universe_from_daily(
    daily: pl.DataFrame,
    *,
    trade_date: date,
    cfg: Config,
    provenance: str,
    candidate_symbols: set[str] | None = None,
    reference_provenance: str | None = None,
) -> pl.DataFrame:
    """Build target-date candidates using only bars strictly before ``trade_date``."""
    required = {"symbol", "trade_date", "high", "low", "close", "volume"}
    missing = required - set(daily.columns)
    if missing:
        raise ValueError(f"daily data missing required columns: {sorted(missing)}")

    history_filter = pl.col("trade_date") < trade_date
    if candidate_symbols is not None:
        # All features are per-symbol except beta, whose only cross-symbol input is
        # SPY. Filtering before rolling calculations preserves exact values while
        # making historical event-pool replay proportional to the locked pool.
        history_filter &= pl.col("symbol").is_in(sorted(candidate_symbols | {"SPY"}))
    history = daily.filter(history_filter).select(*sorted(required)).sort(
        "symbol", "trade_date"
    )
    if history.is_empty():
        raise ValueError(f"no daily history is available before {trade_date.isoformat()}")
    if history.select(pl.struct("symbol", "trade_date").n_unique()).item() != history.height:
        raise ValueError("duplicate (symbol, trade_date) rows in daily history")

    asof_date = history.get_column("trade_date").max()
    if not isinstance(asof_date, date):
        raise ValueError("daily history has no as-of date")
    if history.filter(
        (pl.col("symbol") == "SPY") & (pl.col("trade_date") == asof_date)
    ).is_empty():
        raise ValueError(f"SPY market benchmark is missing on {asof_date}")

    enriched = history.with_columns(
        pl.col("close").shift(1).over("symbol").alias("previous_close")
    ).with_columns(
        (pl.col("close") / pl.col("previous_close") - 1).alias("asset_return"),
        pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - pl.col("previous_close")).abs(),
            (pl.col("low") - pl.col("previous_close")).abs(),
        ).alias("true_range"),
    )
    market_returns = enriched.filter(pl.col("symbol") == "SPY").select(
        "trade_date", pl.col("asset_return").alias("market_return")
    )
    enriched = enriched.join(market_returns, on="trade_date", how="left").with_columns(
        pl.col("true_range")
        .ewm_mean(alpha=1 / ATR_WINDOW, adjust=False, min_samples=ATR_WINDOW)
        .over("symbol")
        .alias("atr"),
        (
            pl.rolling_cov(
                "asset_return",
                "market_return",
                window_size=BETA_WINDOW,
                min_samples=BETA_WINDOW,
            ).over("symbol")
            / pl.col("market_return")
            .rolling_var(window_size=BETA_WINDOW, min_samples=BETA_WINDOW)
            .over("symbol")
        ).alias("beta"),
    )

    session_dates = history.get_column("trade_date").unique().sort()
    adv_dates = session_dates.tail(ADV_WINDOW).to_list()
    adv = (
        history.filter(pl.col("trade_date").is_in(adv_dates))
        .group_by("symbol")
        .agg(
            pl.len().alias("adv_observations"),
            (pl.col("close") * pl.col("volume")).mean().alias("adv_value"),
        )
        .with_columns(
            pl.when(pl.col("adv_observations") == len(adv_dates))
            .then(pl.col("adv_value"))
            .otherwise(None)
            .cast(pl.Float64)
            .alias("adv_usd")
        )
        .select("symbol", "adv_usd")
    )

    current_filter = pl.col("trade_date") == asof_date
    if candidate_symbols is not None:
        current_filter &= pl.col("symbol").is_in(sorted(candidate_symbols))
    current = (
        enriched.filter(current_filter)
        .select(
            "symbol",
            pl.col("close").alias("price"),
            "beta",
            (pl.col("atr") / pl.col("close")).alias("atr_pct"),
        )
        .join(adv, on="symbol", how="left")
        .join(
            enriched.group_by("symbol").agg(
                pl.col("asset_return").abs().max().alias("max_abs_return")
            ),
            on="symbol",
            how="left",
        )
        .sort("symbol")
    )

    decisions = [_reason(row, cfg) for row in current.iter_rows(named=True)]
    result = current.with_columns(
        pl.Series("precheck_pass", [item[0] for item in decisions], dtype=pl.Boolean),
        pl.Series("reject_reason", [item[1] for item in decisions], dtype=pl.String),
        pl.lit(None, dtype=pl.Float64).alias("market_cap"),
        pl.lit(None, dtype=pl.Float64).alias("rvol"),
        pl.lit(None, dtype=pl.String).alias("tier"),
        pl.lit(False).alias("pass_gate"),
        pl.lit(asof_date).cast(pl.Date).alias("asof_date"),
        pl.lit(provenance).alias("price_provenance"),
        pl.lit(f"{provenance}|adv{ADV_WINDOW}.v1").alias("adv_usd_provenance"),
        pl.lit(f"{provenance}|beta{BETA_WINDOW}.v1").alias("beta_provenance"),
        pl.lit(f"{provenance}|wilder_atr{ATR_WINDOW}.v1").alias("atr_pct_provenance"),
        pl.lit(f"{provenance}|max_abs_return.v1").alias(
            "identity_check_provenance"
        ),
        pl.lit("CS" if candidate_symbols is not None else None, dtype=pl.String).alias(
            "security_type"
        ),
        pl.lit(reference_provenance, dtype=pl.String).alias("security_type_provenance"),
    )
    return result.select(*REQUIRED_UNIVERSE_COLUMNS, pl.exclude(REQUIRED_UNIVERSE_COLUMNS))


def _load_accepted_massive_daily(
    trade_date: date, data_root: Path
) -> tuple[pl.DataFrame, str]:
    paths = sorted((data_root / "accepted").glob("massive.grouped_daily-*/data.parquet"))
    if not paths:
        raise FileNotFoundError("no accepted Massive grouped-daily snapshots found")

    path_strings = [str(path) for path in paths]
    available_dates = (
        pl.scan_parquet(path_strings)
        .filter(pl.col("trade_date") < trade_date)
        .select("trade_date")
        .unique()
        .collect(engine="streaming")
        .get_column("trade_date")
        .sort()
    )
    selected_dates = available_dates.tail(LOAD_SESSION_WINDOW).to_list()
    if not selected_dates:
        raise ValueError(f"no accepted Massive data before {trade_date.isoformat()}")

    columns = ["symbol", "trade_date", "high", "low", "close", "volume"]
    daily = (
        pl.scan_parquet(path_strings)
        .filter(pl.col("trade_date").is_in(selected_dates))
        .select(columns)
        .collect(engine="streaming")
    )
    provenance = (
        f"massive.grouped_daily[{len(selected_dates)}sessions]"
        f"@{selected_dates[-1].isoformat()}"
    )
    return daily, provenance


def _load_accepted_common_stocks(
    trade_date: date, data_root: Path
) -> tuple[set[str], str]:
    paths = sorted(
        (data_root / "accepted").glob("massive.reference_tickers.cs-*/data.parquet")
    )
    if not paths:
        raise FileNotFoundError(
            "no accepted point-in-time Massive common-stock reference snapshot found"
        )

    frames = [
        pl.read_parquet(
            path,
            columns=["asof_date", "symbol", "security_type", "active"],
        )
        for path in paths
    ]
    reference = pl.concat(frames)
    eligible_dates = reference.filter(pl.col("asof_date") < trade_date).get_column("asof_date")
    reference_date = eligible_dates.max()
    if not isinstance(reference_date, date):
        raise ValueError(f"no point-in-time common-stock reference before {trade_date}")
    selected = reference.filter(pl.col("asof_date") == reference_date)
    if selected.select(pl.col("symbol").n_unique()).item() != selected.height:
        raise ValueError("duplicate symbols in common-stock reference")
    invalid = selected.filter(
        (pl.col("security_type") != "CS") | (pl.col("active") != True)  # noqa: E712
    ).height
    if invalid:
        raise ValueError(f"common-stock reference contains {invalid} invalid rows")
    symbols = set(selected.get_column("symbol").to_list())
    if not symbols:
        raise ValueError("common-stock reference is empty")
    return symbols, f"massive.reference_tickers.cs@{reference_date.isoformat()}"


def build_universe(
    trade_date: date,
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
    config_path: Path = ROOT / "config.yaml",
) -> pl.DataFrame:
    """Load accepted local data and build a fail-closed target-date universe."""
    daily, provenance = _load_accepted_massive_daily(trade_date, data_root)
    common_stocks, reference_provenance = _load_accepted_common_stocks(trade_date, data_root)
    return _build_universe_from_daily(
        daily,
        trade_date=trade_date,
        cfg=load_config(config_path),
        provenance=provenance,
        candidate_symbols=common_stocks,
        reference_provenance=reference_provenance,
    )
