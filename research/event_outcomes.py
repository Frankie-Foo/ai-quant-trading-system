"""Independent post-session cohorts and explicitly hypothetical response labels."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl

from kernel.event_features import (
    IDENTITY_COLUMNS,
    ResearchSession,
    finite_number,
    frame_records,
    input_hash,
    require_text,
    utc,
)
from kernel.quote_costs import latest_nbbo_spread

LABEL_VERSION = "event_outcomes.v1"
TRADE_COLUMNS = (
    *IDENTITY_COLUMNS, "trade_ts", "available_at", "trade_id", "price", "is_valid",
)
QUOTE_COLUMNS = (
    *IDENTITY_COLUMNS, "ts_utc", "available_at", "bid_price", "ask_price",
    "bid_size", "ask_size", "is_valid",
)


@dataclass(frozen=True)
class CostAssumptions:
    """One hypothetical buy order and one sell order, not an approved fee table."""

    cost_id: str = "us_intraday_conservative.v1"
    commission_per_share_per_side: float = 0.0035
    minimum_per_order_per_side: float = 0.35
    slippage_bps_per_side: float = 10.0
    impact_bps_per_side: float = 0.0
    purpose: str = "research"
    approval_status: str = "unapproved"
    cost_complete: bool = False

    def __post_init__(self) -> None:
        require_text(self.cost_id)
        numeric = (
            self.commission_per_share_per_side, self.minimum_per_order_per_side,
            self.slippage_bps_per_side, self.impact_bps_per_side,
        )
        if not all(finite_number(value) for value in numeric):
            raise ValueError("cost assumptions must be finite and nonnegative")
        if (
            self.commission_per_share_per_side < 0.0035
            or self.minimum_per_order_per_side < 0.35
        ):
            raise ValueError("research commission assumptions cannot be reduced")
        if self.slippage_bps_per_side < 10:
            raise ValueError("research slippage must be at least 10 bps per side")
        if self.slippage_bps_per_side + self.impact_bps_per_side >= 10_000:
            raise ValueError("combined per-side execution adjustment must be below 100%")
        if (
            self.purpose != "research" or self.approval_status != "unapproved"
            or self.cost_complete is not False
        ):
            raise ValueError("this interface cannot approve or certify historical costs")


@dataclass(frozen=True)
class ReplayCosts:
    cost_id: str
    cost_hash: str
    shares: int
    entry_ask: float
    exit_bid: float
    commission_usd: float
    slippage_usd: float
    impact_usd: float
    gross_pnl_usd: float
    research_net_pnl_usd: float
    gross_return: float
    research_net_return: float
    spread_already_in_prices: bool
    assumptions: CostAssumptions


def quote_replay_costs(
    *, entry_ask: float, exit_bid: float, shares: int, costs: CostAssumptions,
) -> ReplayCosts:
    """Subtract commission and additional deviations once; never subtract spread again."""
    if not isinstance(shares, int) or isinstance(shares, bool) or shares <= 0:
        raise ValueError("shares must be a positive integer")
    if not all(finite_number(price, positive=True) for price in (entry_ask, exit_bid)):
        raise ValueError("execution-side prices must be finite and positive")
    entry_notional, exit_notional = shares * entry_ask, shares * exit_bid
    commission = 2 * max(
        shares * costs.commission_per_share_per_side, costs.minimum_per_order_per_side,
    )
    slippage = (entry_notional + exit_notional) * costs.slippage_bps_per_side / 10_000
    impact = (entry_notional + exit_notional) * costs.impact_bps_per_side / 10_000
    gross = exit_notional - entry_notional
    net = gross - commission - slippage - impact
    if not all(finite_number(abs(value)) for value in (
        entry_notional, exit_notional, commission, slippage, impact, gross, net,
    )):
        raise ValueError("nonfinite cost calculation")
    return ReplayCosts(
        cost_id=costs.cost_id, cost_hash=input_hash(asdict(costs)), shares=shares,
        entry_ask=entry_ask, exit_bid=exit_bid, commission_usd=commission,
        slippage_usd=slippage, impact_usd=impact, gross_pnl_usd=gross,
        research_net_pnl_usd=net, gross_return=gross / entry_notional,
        research_net_return=net / entry_notional,
        spread_already_in_prices=True, assumptions=costs,
    )


@dataclass(frozen=True)
class PriceObservation:
    symbol: str
    market_ts: datetime
    market_ts_epoch_ns: int
    available_at: datetime
    price: float
    side: str
    source: str
    feed: str
    price_basis: str
    input_hash: str


@dataclass(frozen=True)
class Top10Row:
    symbol: str
    status: str
    missing_reason: str | None
    p10: PriceObservation | None
    p15: PriceObservation | None
    interval_return: float | None
    rank: int | None = None


@dataclass(frozen=True)
class Top10Cohort:
    cohort_id: str
    cohort_kind: str
    session_id: str
    asof: datetime
    start_utc: datetime
    end_utc: datetime
    status: str
    rows: tuple[Top10Row, ...]
    top10: tuple[Top10Row, ...]
    metadata: dict[str, object]


@dataclass(frozen=True)
class ResponseOutcome:
    event_id: str
    decision_id: str | None
    symbol: str
    response_kind: str
    horizon_minutes: int
    origin_at: datetime
    start_at: datetime | None
    target_at: datetime | None
    asof: datetime
    matured_at: datetime | None
    status: str
    missing_reason: str | None
    entry: PriceObservation | None
    exit: PriceObservation | None
    gross_return: float | None
    research_net_return: float | None
    costs: ReplayCosts | None
    metadata: dict[str, object]


def _market_rows(
    frame: pl.DataFrame, *, kind: str, symbols: set[str], session: ResearchSession,
    asof: datetime, source: str, feed: str, price_basis: str,
) -> list[dict[str, Any]]:
    require_text(source, feed, price_basis)
    stamp = "trade_ts" if kind == "trade" else "ts_utc"
    rows = frame_records(
        frame, columns=TRADE_COLUMNS if kind == "trade" else QUOTE_COLUMNS,
        timestamps=(stamp, "available_at"),
    )
    if frame.schema["is_valid"] != pl.Boolean:
        raise ValueError("is_valid must have Polars Boolean dtype")
    selected = []
    for row in rows:
        if (
            row["symbol"] not in symbols or row["session_id"] != session.session_id
            or (row["source"], row["feed"], row["price_basis"]) != (source, feed, price_basis)
            or not _instant_ns(session.open_utc)
            <= row[f"{stamp}_epoch_ns"] <= _instant_ns(session.close_utc)
            or row["available_at"] > asof or row[stamp] > asof
        ):
            continue
        if row["available_at_epoch_ns"] < row[f"{stamp}_epoch_ns"]:
            raise ValueError("available_at cannot precede the market observation")
        if row["is_valid"] is None:
            raise ValueError("is_valid must be explicit, not null")
        if kind == "trade" and (
            type(row["trade_id"]) not in (int, str) or row["trade_id"] == ""
        ):
            raise ValueError("trade_id must be a nonempty string or integer")
        selected.append(row)
    # ponytail: one-session in-memory records; partition upstream for large universes.
    return sorted(
        selected,
        key=lambda row: (row["symbol"], row[f"{stamp}_epoch_ns"], input_hash(row)),
    )


def _instant_ns(value: datetime) -> int:
    delta = utc(value) - datetime(1970, 1, 1, tzinfo=UTC)
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1000


def _observation(
    row: dict[str, Any], *, kind: str, side: str,
) -> PriceObservation:
    stamp = "trade_ts" if kind == "trade" else "ts_utc"
    price = row["price"] if kind == "trade" else row[f"{side}_price"]
    return PriceObservation(
        symbol=row["symbol"], market_ts=row[stamp],
        market_ts_epoch_ns=row[f"{stamp}_epoch_ns"],
        available_at=row["available_at"], price=float(price), side=side,
        source=row["source"], feed=row["feed"], price_basis=row["price_basis"],
        input_hash=input_hash(row),
    )


def _trade_at(
    rows: list[dict[str, Any]], *, at: datetime,
) -> tuple[PriceObservation | None, str | None]:
    eligible = [row for row in rows if (
        _instant_ns(at - timedelta(seconds=60)) <= row["trade_ts_epoch_ns"] <= _instant_ns(at)
        and row["available_at"] <= at and row["is_valid"]
        and finite_number(row["price"], positive=True)
    )]
    if not eligible:
        return None, "no_valid_trade_within_60s_known_at_boundary"
    latest = max(row["trade_ts_epoch_ns"] for row in eligible)
    tied = [row for row in eligible if row["trade_ts_epoch_ns"] == latest]
    if len({row["price"] for row in tied}) != 1:
        return None, "ambiguous_trade_timestamp"
    row = min(tied, key=lambda row: (row["available_at_epoch_ns"], input_hash(row)))
    return _observation(row, kind="trade", side="trade"), None


def _quote_at(
    rows: list[dict[str, Any]], *, at: datetime, side: str, shares: int, max_age: timedelta,
) -> tuple[PriceObservation | None, str | None]:
    eligible = [row for row in rows if row["ts_utc"] <= at and row["available_at"] <= at]
    if not eligible:
        return None, "missing_quote_known_at_boundary"
    latest = max(row["ts_utc_epoch_ns"] for row in eligible)
    tied = [row for row in eligible if row["ts_utc_epoch_ns"] == latest]
    quote_fields = ("bid_price", "ask_price", "bid_size", "ask_size", "is_valid")
    if len({input_hash({key: row[key] for key in quote_fields}) for row in tied}) != 1:
        return None, "ambiguous_quote_timestamp"
    row = min(tied, key=lambda row: (row["available_at_epoch_ns"], input_hash(row)))
    if row["ts_utc_epoch_ns"] < _instant_ns(at - max_age):
        return None, "stale_quote"
    if (
        not row["is_valid"] or not finite_number(row["bid_price"], positive=True)
        or not finite_number(row["ask_price"], positive=True)
        or row["bid_price"] > row["ask_price"]
    ):
        return None, "invalid_or_crossed_quote"
    if (
        not finite_number(row[f"{side}_size"], positive=True)
        or row[f"{side}_size"] < shares
    ):
        return None, "insufficient_displayed_size"
    # Reuse only after the stricter availability, identity and capacity checks.
    observation = latest_nbbo_spread(
        pl.DataFrame([{
            "symbol": row["symbol"], "ts_utc": row["ts_utc"],
            "bid_price": float(row["bid_price"]), "ask_price": float(row["ask_price"]),
            "source": row["source"], "feed": row["feed"],
        }]), symbol=row["symbol"], at_utc=at, max_age=max_age,
    )
    if observation is None:
        return None, "invalid_quote"
    return _observation(row, kind="quote", side=side), None


def build_top10_cohorts(
    trades: pl.DataFrame, *, discovery_symbols: Iterable[str], tradable_symbols: Iterable[str],
    discovery_cohort_id: str, tradable_cohort_id: str, session: ResearchSession,
    asof: datetime, source: str, feed: str, price_basis: str, validity_policy_id: str,
) -> tuple[Top10Cohort, Top10Cohort]:
    """Rank two caller-owned 10:00–15:00 ET cohorts; retain every missing member."""
    require_text(discovery_cohort_id, tradable_cohort_id, validity_policy_id)
    if discovery_cohort_id == tradable_cohort_id:
        raise ValueError("cohort IDs must be distinct")
    if isinstance(discovery_symbols, str) or isinstance(tradable_symbols, str):
        raise ValueError("cohorts must be iterables of symbols, not a string")
    discovery, tradable = set(discovery_symbols), set(tradable_symbols)
    require_text(*discovery, *tradable)
    if not tradable <= discovery:
        raise ValueError("tradable cohort must be a subset of discovery cohort")
    asof = utc(asof)
    start = session.open_utc + timedelta(minutes=30)
    end = session.open_utc + timedelta(hours=5, minutes=30)
    rows = _market_rows(
        trades, kind="trade", symbols=discovery, session=session,
        asof=asof, source=source, feed=feed, price_basis=price_basis,
    )
    by_symbol: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in discovery}
    for row in rows:
        by_symbol[row["symbol"]].append(row)
    outcomes: dict[str, Top10Row] = {}
    for symbol in sorted(discovery):
        symbol_rows = by_symbol[symbol]
        p10, reason10 = (
            _trade_at(symbol_rows, at=start) if asof >= start else (None, "not_matured")
        )
        p15, reason15 = (
            _trade_at(symbol_rows, at=end) if asof >= end else (None, "not_matured")
        )
        if session.close_utc < end:
            status, reason = "short_session", "full_10_15_window_absent"
        elif asof < end:
            status, reason = "not_matured", "asof_before_15_et"
        elif reason10 or reason15:
            status, reason = "missing_price", ";".join(
                f"{name}:{value}"
                for name, value in (("p10", reason10), ("p15", reason15)) if value
            )
        else:
            status, reason = "available", None
        value = p15.price / p10.price - 1 if status == "available" and p10 and p15 else None
        if value is not None and not finite_number(abs(value)):
            value, status, reason = None, "missing_price", "nonfinite_return"
        outcomes[symbol] = Top10Row(symbol, status, reason, p10, p15, value)

    results = []
    for cohort_id, kind, members in (
        (discovery_cohort_id, "discovery", discovery),
        (tradable_cohort_id, "tradable", tradable),
    ):
        valid = sorted(
            (outcomes[s] for s in members if outcomes[s].interval_return is not None),
            key=lambda row: (-float(row.interval_return or 0.0), row.symbol),
        )
        ranked = {row.symbol: replace(row, rank=rank) for rank, row in enumerate(valid, 1)}
        member_rows = tuple(ranked.get(s, outcomes[s]) for s in sorted(members))
        status = (
            "short_session" if session.close_utc < end else "not_matured" if asof < end
            else "empty_cohort" if not members
            else "available" if len(valid) == len(members) else "partial"
        )
        metadata = {
            "label_version": LABEL_VERSION,
            "source": source, "feed": feed, "price_basis": price_basis,
            "validity_policy_id": validity_policy_id, "expected_symbols": len(members),
            "priced_symbols": len(valid), "missing_symbols": len(members) - len(valid),
            "coverage_fraction": len(valid) / len(members) if members else None,
            "universe_scope": "caller_supplied_not_certified_full_market",
            "boundary_availability_rule": "available_at<=boundary AND available_at<=asof",
            "input_hash": input_hash({
                "version": LABEL_VERSION, "cohort_id": cohort_id, "kind": kind,
                "members": sorted(members), "session": asdict(session), "asof": asof,
                "source": source, "feed": feed, "price_basis": price_basis,
                "validity_policy_id": validity_policy_id,
                "rows": [row for row in rows if row["symbol"] in members],
            }),
        }
        results.append(Top10Cohort(
            cohort_id, kind, session.session_id, asof, start, end, status, member_rows,
            tuple(ranked[row.symbol] for row in valid[:10]), metadata,
        ))
    return results[0], results[1]


def _horizons(values: tuple[int, ...]) -> tuple[int, ...]:
    if not values or any(
        type(value) is not int or value not in (15, 30, 60) for value in values
    ):
        raise ValueError("horizons must be a nonempty tuple drawn from 15, 30, 60 minutes")
    if len(set(values)) != len(values):
        raise ValueError("duplicate horizons are not allowed")
    return values


def _label(
    *, event_id: str, decision_id: str | None, symbol: str, kind: str, horizon: int,
    origin: datetime, start: datetime | None, asof: datetime, session: ResearchSession,
    entry: PriceObservation | None, exit: PriceObservation | None,
    reason: str | None, metadata: Mapping[str, object], costs: ReplayCosts | None = None,
) -> ResponseOutcome:
    """Require an origin at or after 10:00 ET; never shift an earlier origin."""
    target = start + timedelta(minutes=horizon) if start is not None else None
    if origin < session.open_utc + timedelta(minutes=30) or origin >= session.research_end_utc:
        status, reason = "outside_research_window", "origin_outside_regular_research_session"
    elif (target or origin + timedelta(minutes=horizon)) > session.research_end_utc:
        status, reason = "right_censored", "horizon_exceeds_same_session_research_end"
    elif asof < origin or target is not None and asof < target:
        status, reason = "not_matured", "asof_before_origin_or_target"
    elif reason:
        status = "missing_price"
    elif entry is None or exit is None:
        status, reason = "missing_price", "missing_boundary_observation"
    else:
        status = "available"
    mature = (
        max(target, entry.available_at, exit.available_at)
        if status == "available" and target and entry and exit else None
    )
    gross = exit.price / entry.price - 1 if status == "available" and entry and exit else None
    if gross is not None and not finite_number(abs(gross)):
        gross, mature, status, reason = None, None, "missing_price", "nonfinite_return"
    return ResponseOutcome(
        event_id, decision_id, symbol, kind, horizon, origin, start, target, asof, mature,
        status, reason, entry, exit, gross,
        costs.research_net_return if status == "available" and costs else None,
        costs if status == "available" else None, dict(metadata),
    )


def event_response(
    trades: pl.DataFrame, *, event_id: str, symbol: str, recognized_at: datetime,
    session: ResearchSession, asof: datetime, source: str, feed: str, price_basis: str,
    validity_policy_id: str, horizons: tuple[int, ...] = (15, 30, 60),
) -> tuple[ResponseOutcome, ...]:
    """Gross trade-price response from caller-evidenced first recognizable instant."""
    require_text(event_id, symbol, validity_policy_id)
    origin, asof = utc(recognized_at), utc(asof)
    rows = _market_rows(
        trades, kind="trade", symbols={symbol}, session=session,
        asof=asof, source=source, feed=feed, price_basis=price_basis,
    )
    metadata = {
        "label_version": LABEL_VERSION, "session": asdict(session), "source": source,
        "feed": feed, "price_basis": price_basis, "validity_policy_id": validity_policy_id,
        "origin_semantics": "caller_evidenced_first_recognizable_instant_not_publication",
        "execution_kind": "event_price_response_not_a_fill", "cost_status": "not_applicable",
        "path_metrics_status": "not_computed_no_path_coverage_contract",
        "input_hash": input_hash({
            "version": LABEL_VERSION, "kind": "event_response", "event_id": event_id,
            "symbol": symbol, "origin": origin, "asof": asof, "session": asdict(session),
            "source": source, "feed": feed, "price_basis": price_basis,
            "validity_policy_id": validity_policy_id, "horizons": horizons, "rows": rows,
        }),
    }
    entry, entry_reason = _trade_at(rows, at=origin)
    result = []
    for horizon in _horizons(horizons):
        target = origin + timedelta(minutes=horizon)
        exit, exit_reason = (None, None)
        if target <= min(asof, session.research_end_utc):
            exit, exit_reason = _trade_at(rows, at=target)
        reason = (
            f"entry:{entry_reason}" if entry_reason
            else f"exit:{exit_reason}" if exit_reason else None
        )
        result.append(_label(
            event_id=event_id, decision_id=None, symbol=symbol, kind="event_response",
            horizon=horizon, origin=origin, start=origin, asof=asof, session=session,
            entry=entry, exit=exit, reason=reason, metadata=metadata,
        ))
    return tuple(result)


def decision_response(
    quotes: pl.DataFrame, *, event_id: str, decision_id: str, symbol: str,
    decision_at: datetime, session: ResearchSession, asof: datetime,
    source: str, feed: str, price_basis: str, validity_policy_id: str,
    shares: int, costs: CostAssumptions, horizons: tuple[int, ...] = (15, 30, 60),
    max_entry_wait: timedelta = timedelta(minutes=1),
    max_quote_age: timedelta = timedelta(seconds=30),
) -> tuple[ResponseOutcome, ...]:
    """First executable ask, then fixed-horizon bid; hypothetical orders only."""
    require_text(event_id, decision_id, symbol, validity_policy_id)
    origin, asof = utc(decision_at), utc(asof)
    if type(shares) is not int or shares <= 0:
        raise ValueError("shares must be a positive integer")
    if not timedelta(0) < max_entry_wait <= timedelta(minutes=1):
        raise ValueError("max_entry_wait must be positive and at most one minute")
    if not timedelta(0) < max_quote_age <= timedelta(seconds=30):
        raise ValueError("max_quote_age must be positive and at most 30 seconds")
    rows = _market_rows(
        quotes, kind="quote", symbols={symbol}, session=session,
        asof=asof, source=source, feed=feed, price_basis=price_basis,
    )
    deadline = min(origin + max_entry_wait, session.research_end_utc)
    entry = None
    start = None
    entry_reason: str | None = "no_executable_entry_quote"
    instants = {origin} | {
        row["available_at"] for row in rows if origin <= row["available_at"] <= deadline
    }
    # Replay only visibility transitions, always choosing the newest market update
    # known then. A late obsolete update must not resurrect a superseded ask.
    for instant in sorted(instants):
        if (
            not session.open_utc + timedelta(minutes=30)
            <= origin <= instant < session.research_end_utc
            or instant > asof
        ):
            continue
        entry, entry_reason = _quote_at(
            rows, at=instant, side="ask", shares=shares, max_age=max_quote_age,
        )
        if entry is not None:
            start = instant
            break
    metadata = {
        "label_version": LABEL_VERSION, "session": asdict(session), "source": source,
        "feed": feed, "price_basis": price_basis, "validity_policy_id": validity_policy_id,
        "origin_semantics": "decision_at",
        "start_semantics": "first_executable_ask_known_at_or_after_decision",
        "exit_rule_id": "fixed_elapsed_horizon_bid.v1",
        "execution_kind": "quote_replay_not_actual_fill",
        "quote_size_unit": "shares", "cost_assumptions": asdict(costs),
        "cost_hash": input_hash(asdict(costs)), "formal_profitability_eligible": False,
        "path_metrics_status": "not_computed_no_path_coverage_contract",
        "max_entry_wait_seconds": max_entry_wait.total_seconds(),
        "max_quote_age_seconds": max_quote_age.total_seconds(),
        "input_hash": input_hash({
            "version": LABEL_VERSION, "kind": "decision_response", "event_id": event_id,
            "decision_id": decision_id, "symbol": symbol, "origin": origin, "asof": asof,
            "session": asdict(session), "source": source, "feed": feed, "price_basis": price_basis,
            "validity_policy_id": validity_policy_id, "shares": shares, "costs": asdict(costs),
            "horizons": horizons, "max_entry_wait_seconds": max_entry_wait.total_seconds(),
            "max_quote_age_seconds": max_quote_age.total_seconds(), "rows": rows,
        }),
    }
    result = []
    for horizon in _horizons(horizons):
        exit = None
        reason: str | None = entry_reason
        breakdown = None
        target = start + timedelta(minutes=horizon) if start else None
        if target is not None and target <= min(asof, session.research_end_utc):
            exit, reason = _quote_at(
                rows, at=target, side="bid", shares=shares, max_age=max_quote_age,
            )
            if entry is not None and exit is not None:
                breakdown = quote_replay_costs(
                    entry_ask=entry.price, exit_bid=exit.price, shares=shares, costs=costs,
                )
        outcome = _label(
            event_id=event_id, decision_id=decision_id, symbol=symbol, kind="decision_response",
            horizon=horizon, origin=origin, start=start, asof=asof, session=session,
            entry=entry, exit=exit, reason=reason, metadata=metadata, costs=breakdown,
        )
        if start is None and outcome.status == "missing_price" and asof < deadline:
            outcome = replace(
                outcome, status="not_matured", missing_reason="entry_window_still_open",
            )
        result.append(outcome)
    return tuple(result)
