"""Frozen point-in-time historical selection profile for causal research replay."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import polars as pl

from data_plane.calendar import build_xnys_schedule
from kernel.catalysts import build_catalyst_candidates, select_overnight_catalysts
from kernel.config import Config
from kernel.universe import _build_universe_from_daily

BEIJING = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")
HISTORICAL_SELECTION_PROFILE = "massive_news_only.v1"


def target_sessions(*, end_date: date, sessions: int) -> tuple[date, ...]:
    if sessions <= 0:
        raise ValueError("sessions must be positive")
    start = end_date - timedelta(days=max(400, sessions * 2))
    values = build_xnys_schedule(start, end_date).get_column("trade_date").to_list()
    if len(values) < sessions:
        raise ValueError("not enough XNYS sessions in calendar window")
    return tuple(values[-sessions:])


def catalyst_lock_asof_utc(trade_date: date) -> datetime:
    """Beijing 08:00 lock, exactly matching the online overnight-news policy."""
    return datetime.combine(trade_date, time(8), BEIJING).astimezone(UTC)


def premarket_decision_asof_utc(trade_date: date) -> datetime:
    """Beijing 20:00 selection time, matching the configured online gate."""
    return datetime.combine(trade_date, time(20), BEIJING).astimezone(UTC)


def premarket_data_cutoff_utc(
    trade_date: date,
    *,
    provider_delay_minutes: int = 0,
) -> datetime:
    """Return the causal cutoff for the explicitly selected market-data policy."""
    if provider_delay_minutes < 0:
        raise ValueError("provider delay must not be negative")
    return premarket_decision_asof_utc(trade_date) - timedelta(
        minutes=provider_delay_minutes
    )


def premarket_feature_cutoff_et(
    trade_date: date,
    *,
    provider_delay_minutes: int = 0,
) -> time:
    """Return the target session's New York wall-time cutoff, including DST."""
    cutoff = premarket_data_cutoff_utc(
        trade_date,
        provider_delay_minutes=provider_delay_minutes,
    ).astimezone(NEW_YORK)
    if cutoff.date() != trade_date:
        raise ValueError("premarket data cutoff must fall on the target New York date")
    return cutoff.time().replace(tzinfo=None)


def required_premarket_symbols(
    candidate_symbols: dict[date, tuple[str, ...]],
    *,
    schedule: pl.DataFrame,
    history_sessions: int = 20,
) -> dict[date, tuple[str, ...]]:
    """Plan minimal session queries shared across overlapping RVOL lookbacks."""
    if history_sessions <= 0:
        raise ValueError("history_sessions must be positive")
    schedule_dates = schedule.get_column("trade_date").to_list()
    required: dict[date, set[str]] = {}
    for target, symbols in sorted(candidate_symbols.items()):
        prior = [value for value in schedule_dates if value <= target]
        window = prior[-(history_sessions + 1) :]
        if len(window) != history_sessions + 1 or window[-1] != target:
            raise ValueError(f"target plus history are unavailable for {target}")
        normalized = {value.strip().upper() for value in symbols if value.strip()}
        for session_date in window:
            required.setdefault(session_date, set()).update(normalized)
    return {
        session_date: tuple(sorted(symbols))
        for session_date, symbols in sorted(required.items())
        if symbols
    }


def build_pit_selection_session(
    *,
    daily: pl.DataFrame,
    reference: pl.DataFrame,
    prepared_news: pl.DataFrame,
    schedule: pl.DataFrame,
    trade_date: date,
    cfg: Config,
    daily_provenance: str,
    reference_provenance: str,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Build one lock using only the frozen Massive-news strategy source profile."""
    reference_dates = reference.get_column("asof_date").unique().to_list()
    if len(reference_dates) != 1 or reference_dates[0] >= trade_date:
        raise ValueError("reference snapshot must have one date strictly before trade date")
    overnight = select_overnight_catalysts(
        prepared_news,
        schedule=schedule,
        target_date=trade_date,
        asof_utc=catalyst_lock_asof_utc(trade_date),
    )
    event_symbols: set[str] = set()
    for value in overnight.get_column("symbols").to_list():
        if isinstance(value, list):
            event_symbols.update(str(item) for item in value)
    active_common_stocks = set(reference.get_column("symbol").to_list())
    requested = event_symbols.intersection(active_common_stocks)
    if not requested:
        # Preserve the session/as-of envelope for a valid no-catalyst day. SPY is
        # only a benchmark row and cannot enter candidates without an event.
        requested = {"SPY"}
    universe = _build_universe_from_daily(
        daily,
        trade_date=trade_date,
        cfg=cfg,
        provenance=daily_provenance,
        candidate_symbols=requested,
        reference_provenance=reference_provenance,
    )
    candidates = build_catalyst_candidates(universe, overnight).with_columns(
        pl.lit(HISTORICAL_SELECTION_PROFILE).alias("selection_profile"),
        pl.lit(catalyst_lock_asof_utc(trade_date))
        .cast(pl.Datetime("ms", "UTC"))
        .alias("lock_asof_utc"),
    )
    return universe, candidates
