"""Pure, frozen research statistics; no adapters, fitting, or trading actions."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from math import fsum, isfinite
from typing import Annotated, Literal, Self, TypeVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(min_length=1, pattern=r"^\S+(?: \S+)*$")]
Ratio = Annotated[float, Field(ge=0, le=1)]
SampleKey = tuple[str, str, date]


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamps must be timezone-aware UTC")
    return value


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


FrozenT = TypeVar("FrozenT", bound=_Frozen)


class Provenance(_Frozen):
    source_kind: Literal["market_data", "research_replay", "broker_fill", "manual"]
    source_id: Identifier
    snapshot_id: Identifier
    evidence_ids: tuple[Identifier, ...] = Field(min_length=1)
    quality: Literal["passed"]


class TradingSession(_Frozen):
    """Caller-supplied exchange calendar; never inferred from weekdays."""

    session_date: date
    open_at: datetime
    close_at: datetime

    _times = field_validator("open_at", "close_at")(_utc)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if not self.open_at < self.close_at:
            raise ValueError("session open must precede close")
        if self.open_at.date() != self.session_date or self.close_at.date() != self.session_date:
            raise ValueError("US equity session timestamps must match session_date in UTC")
        return self


class CostApproval(_Frozen):
    cost_model_id: Identifier
    approved_at: datetime
    approved_by: Identifier
    evidence_id: Identifier

    _time = field_validator("approved_at")(_utc)


class _Record(_Frozen):
    event_cluster_id: Identifier
    symbol: Annotated[str, Field(min_length=1, pattern=r"^[A-Z0-9][A-Z0-9.\-/]*$")]
    session_date: date
    factor: Identifier
    observed_at: datetime
    matured_at: datetime | None
    coverage: Ratio
    provenance: Provenance

    @field_validator("observed_at", "matured_at")
    @classmethod
    def utc_times(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def chronology(self) -> Self:
        if self.matured_at is not None and self.matured_at < self.observed_at:
            raise ValueError("matured_at must include observation/publication latency")
        return self

    @property
    def key(self) -> SampleKey:
        return self.event_cluster_id, self.symbol, self.session_date


RecordT = TypeVar("RecordT", bound=_Record)


class EventRecord(_Record):
    """One primary factor and one fixed label definition per event/symbol/day."""

    source: Literal["all_events"]
    label_definition_id: Identifier
    label_origin: Literal["event_response", "decision_response", "actual_fill"]
    horizon_minutes: Literal[15, 30, 60]
    label_start_at: datetime
    cost_model_id: Identifier
    net_return: float | None
    mfe: Annotated[float, Field(ge=0)] | None
    mae: Annotated[float, Field(le=0)] | None
    missing_reason: Identifier | None = None

    _start_time = field_validator("label_start_at")(_utc)

    @model_validator(mode="after")
    def label_availability(self) -> Self:
        if self.net_return is None and self.missing_reason is None:
            raise ValueError("missing net_return requires missing_reason")
        if self.net_return is not None and self.missing_reason is not None:
            raise ValueError("observed net_return cannot have missing_reason")
        if self.matured_at is None and any(
            value is not None for value in (self.net_return, self.mfe, self.mae)
        ):
            raise ValueError("unmatured records cannot contain outcome values")
        if self.label_origin == "actual_fill" and self.provenance.source_kind not in (
            "broker_fill", "manual"
        ):
            raise ValueError("actual_fill requires broker or separate manual evidence")
        return self


class Top10Record(_Record):
    source: Literal["postclose_top10"]
    rank: Annotated[int, Field(ge=1, le=10)]
    interval_return: float | None
    missing_reason: Identifier | None = None

    @model_validator(mode="after")
    def label_availability(self) -> Self:
        if (self.interval_return is None) != (self.missing_reason is not None):
            raise ValueError("missing return requires a reason, observed return forbids it")
        if self.matured_at is None and self.interval_return is not None:
            raise ValueError("unmatured Top10 cannot contain return")
        return self


class EdgeStatistics(_Frozen):
    factor: str | None
    total_count: int
    count: int
    missing_count: int
    unmatured_count: int
    wins: int
    losses: int
    flat_count: int
    independent_events: int
    independent_days: int
    win_rate: float | None
    mean: float | None
    median: float | None
    avg_win: float | None
    avg_loss: float | None
    profit_factor: float | None
    expectancy: float | None
    shrinkage_weight: float
    shrunk_expectancy: float | None
    coverage: float | None
    source_coverage: float | None
    mean_mfe: float | None
    mean_mae: float | None
    missing_mfe_count: int
    missing_mae_count: int
    sample_keys: tuple[SampleKey, ...]
    reasons: tuple[str, ...]
    expectancy_ci95: None = None
    ci_method: Literal["not_estimated"] = "not_estimated"
    promotion_eligible: Literal[False] = False
    production_eligible: Literal[False] = False


class FactorHeat(_Frozen):
    factor: str
    window_sessions: Literal[3, 5, 10]
    available_sessions: int
    represented_sessions: int
    event_count: int
    count: int
    missing_count: int
    unmatured_count: int
    score: float | None
    daily_mean_score: float | None
    coverage: float | None
    source_coverage: float | None
    reasons: tuple[str, ...]
    score_scope: Literal["observed_heat_only"] = "observed_heat_only"
    normalization: Literal["explicit_window_session_count"] = "explicit_window_session_count"
    complete_market_estimate: Literal[False] = False


class EventEdgeReport(_Frozen):
    decision_session: date
    freeze_at: datetime
    trading_sessions: tuple[TradingSession, ...]
    baseline_dates: tuple[date, ...]
    cost_approvals: tuple[CostApproval, ...]
    event_records: tuple[EventRecord, ...]
    top10_records: tuple[Top10Record, ...]
    event_duplicates_removed: int
    top10_duplicates_removed: int
    overall: EdgeStatistics
    factors: tuple[EdgeStatistics, ...]
    heat: tuple[FactorHeat, ...]
    method: Literal["event_edge_v1_n30_baseline60"] = "event_edge_v1_n30_baseline60"
    promotion_eligible: Literal[False] = False
    production_eligible: Literal[False] = False


def _mean(values: Sequence[float]) -> float | None:
    # Divide before summing to avoid overflow from otherwise finite returns.
    return fsum(value / len(values) for value in values) if values else None


def _ratio(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    result = numerator / denominator
    return result if isfinite(result) else None


def _unique[T: _Record](records: Sequence[T]) -> tuple[tuple[T, ...], int]:
    by_key: dict[SampleKey, T] = {}
    for record in records:
        previous = by_key.get(record.key)
        if previous is not None and previous != record:
            raise ValueError(f"conflicting event/symbol/day record: {record.key}")
        by_key[record.key] = record
    return tuple(by_key[key] for key in sorted(by_key)), len(records) - len(by_key)


def _statistics(
    rows: Sequence[EventRecord], *, factor: str | None, prior: float | None,
    baseline_length: int, approved_costs: set[str],
) -> EdgeStatistics:
    values = [row.net_return for row in rows if row.net_return is not None]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    measured = [row for row in rows if row.net_return is not None]
    events = len({row.event_cluster_id for row in measured})
    days = len({row.session_date for row in measured})
    mean = _mean(values)
    weight = events / (events + 30)
    shrunk = (
        weight * mean + (1 - weight) * prior
        if mean is not None and prior is not None else None
    )
    reasons = ["research_only", "ci_not_estimated", "population_completeness_not_verified"]
    if baseline_length < 60:
        reasons.append("baseline_fewer_than_60_sessions")
    if events < 30:
        reasons.append("fewer_than_30_independent_events")
    if days < 5:
        reasons.append("fewer_than_5_independent_days")
    if not values:
        reasons.append("no_observed_returns")
    elif not losses:
        reasons.append("no_loss_samples_selection_bias_unresolved")
        if len(wins) == len(values):
            reasons.append("all_winners_selection_bias_unresolved")
    if any(row.cost_model_id not in approved_costs for row in rows):
        reasons.append("cost_not_approved_at_freeze")
    if any(row.provenance.source_kind == "manual" for row in rows):
        reasons.append("manual_evidence_not_automatic_execution_proof")
    if len(values) < len(rows) or any(row.coverage < 1 for row in rows):
        reasons.append("incomplete_coverage")
    if any(row.factor == "unknown" for row in rows):
        reasons.append("unknown_factor")
    # PF uses scaled sums, preserving the ratio without overflowing gross totals.
    scale = max((abs(value) for value in values), default=0.0)
    profit_factor = _ratio(
        fsum(value / scale for value in wins) if scale else 0.0,
        fsum(-value / scale for value in losses) if scale else 0.0,
    )
    ordered_values = sorted(values)
    middle = len(values) // 2
    median_value = (
        ordered_values[middle] if len(values) % 2
        else _mean(ordered_values[middle - 1:middle + 1])
    ) if values else None
    return EdgeStatistics(
        factor=factor, total_count=len(rows), count=len(values),
        missing_count=sum(row.matured_at is not None and row.net_return is None for row in rows),
        unmatured_count=sum(row.matured_at is None for row in rows),
        wins=len(wins), losses=len(losses), flat_count=len(values) - len(wins) - len(losses),
        independent_events=events, independent_days=days,
        win_rate=_ratio(len(wins), len(values)), mean=mean,
        median=median_value, avg_win=_mean(wins), avg_loss=_mean(losses),
        profit_factor=profit_factor, expectancy=mean,
        shrinkage_weight=weight, shrunk_expectancy=shrunk if factor is not None else mean,
        coverage=_ratio(len(values), len(rows)), source_coverage=_mean([r.coverage for r in rows]),
        mean_mfe=_mean([r.mfe for r in rows if r.mfe is not None]),
        mean_mae=_mean([r.mae for r in rows if r.mae is not None]),
        missing_mfe_count=sum(r.mfe is None for r in rows),
        missing_mae_count=sum(r.mae is None for r in rows),
        sample_keys=tuple(r.key for r in measured), reasons=tuple(reasons),
    )


def build_event_edge(
    *, records: Sequence[EventRecord], top10_records: Sequence[Top10Record],
    trading_sessions: Sequence[TradingSession], decision_session: date,
    freeze_at: datetime, cost_approvals: Sequence[CostApproval],
) -> EventEdgeReport:
    """Build a pre-open snapshot; future evidence is an error, not a filter.

    Supply exactly one label/cost/evidence-kind population per invocation.
    Pending labels have matured_at=None and no outcomes. All rows remain in
    the report; only preceding 60 supplied sessions feed expectancy.
    """
    _utc(freeze_at)
    # Revalidate even model_construct/model_copy inputs at this trust boundary.
    def validated(items: Sequence[FrozenT], model: type[FrozenT]) -> tuple[FrozenT, ...]:
        if any(type(item) is not model for item in items):
            raise ValueError(f"expected explicit {model.__name__} records")
        return tuple(model.model_validate(item.model_dump()) for item in items)

    sessions = validated(trading_sessions, TradingSession)
    if not sessions or type(decision_session) is not date:
        raise ValueError("explicit calendar and date decision_session are required")
    dates = tuple(session.session_date for session in sessions)
    if dates != tuple(sorted(set(dates))) or decision_session not in dates:
        raise ValueError("calendar must be ordered, unique, and include decision_session")
    calendar = {session.session_date: session for session in sessions}
    if freeze_at > calendar[decision_session].open_at:
        raise ValueError("snapshot must freeze no later than decision session open")
    earlier = dates[:dates.index(decision_session)]
    if any(calendar[day].close_at >= freeze_at for day in earlier):
        raise ValueError("preceding sessions must be closed before freeze")
    approvals = validated(cost_approvals, CostApproval)
    if any(approval.approved_at >= freeze_at for approval in approvals):
        raise ValueError("cost approval must be known strictly before freeze")
    if len({a.cost_model_id for a in approvals}) != len(approvals):
        raise ValueError("duplicate cost approval; resolve revision explicitly")
    events, event_duplicates = _unique(validated(records, EventRecord))
    discovery, top_duplicates = _unique(validated(top10_records, Top10Record))
    for row in (*events, *discovery):
        if row.session_date not in earlier:
            raise ValueError("record session must be explicitly listed before decision session")
        session = calendar[row.session_date]
        if row.observed_at >= freeze_at or (
            row.matured_at is not None and row.matured_at >= freeze_at
        ):
            raise ValueError("future or freeze-boundary evidence is forbidden")
        if isinstance(row, EventRecord):
            if not session.open_at <= row.label_start_at < session.close_at:
                raise ValueError("label start must be within its explicit regular session")
            window_end = session.close_at
            if row.label_origin in ("event_response", "decision_response"):
                market_zone = ZoneInfo("America/New_York")
                research_start = datetime.combine(
                    row.session_date, time(10), tzinfo=market_zone,
                ).astimezone(UTC)
                research_end = datetime.combine(
                    row.session_date, time(15), tzinfo=market_zone,
                ).astimezone(UTC)
                window_end = min(research_end, session.close_at)
                if not max(research_start, session.open_at) <= row.label_start_at < window_end:
                    raise ValueError(
                        "research label start must be within 10:00 ET to min(15:00 ET, close)"
                    )
            # actual_fill is an externally normalized fixed-horizon label, not
            # an execution/holding-period label generated or verified here.
            if row.label_start_at >= freeze_at:
                raise ValueError("future label start is forbidden")
            if row.matured_at is not None and row.matured_at < session.open_at:
                raise ValueError("intraday label cannot mature before session open")
            if any(value is not None for value in (row.net_return, row.mfe, row.mae)):
                end_at = row.label_start_at + timedelta(minutes=row.horizon_minutes)
                if (
                    row.matured_at is None
                    or end_at > window_end
                    or row.matured_at < end_at
                ):
                    raise ValueError(
                        "outcomes require a complete origin-specific horizon before maturity"
                    )
        elif row.observed_at < session.close_at:
            raise ValueError("postclose Top10 cannot be observed before session close")
    populations = {
        (r.label_definition_id, r.label_origin, r.horizon_minutes,
         r.cost_model_id, r.provenance.source_kind) for r in events
    }
    if len(populations) > 1:
        raise ValueError("mixed label/cost/evidence populations require separate reports")
    slots: set[tuple[date, int]] = set()
    symbols: set[tuple[date, str]] = set()
    for row in discovery:
        if (row.session_date, row.rank) in slots or (row.session_date, row.symbol) in symbols:
            raise ValueError("Top10 rank and symbol must each be unique per session")
        slots.add((row.session_date, row.rank))
        symbols.add((row.session_date, row.symbol))
    baseline_dates = earlier[-60:]
    baseline = [row for row in events if row.session_date in baseline_dates]
    approved = {approval.cost_model_id for approval in approvals}
    overall = _statistics(
        baseline,
        factor=None,
        prior=None,
        baseline_length=len(baseline_dates),
        approved_costs=approved,
    )
    grouped: dict[str, list[EventRecord]] = defaultdict(list)
    for row in baseline:
        grouped[row.factor].append(row)
    factors = tuple(
        _statistics(grouped[factor], factor=factor, prior=overall.mean,
                    baseline_length=len(baseline_dates), approved_costs=approved)
        for factor in sorted(grouped)
    )
    heat = []
    heat_factors = sorted({row.factor for row in discovery if row.session_date in earlier[-10:]})
    for window in (3, 5, 10):
        window_dates = earlier[-window:]
        for factor in heat_factors:
            rows = [r for r in discovery if r.factor == factor and r.session_date in window_dates]
            values = [r for r in rows if r.interval_return is not None]
            reasons = ["discovery_only_not_expectancy", "population_completeness_not_verified"]
            if len(window_dates) < window:
                reasons.append("insufficient_calendar_history")
            if len(values) < len(rows):
                reasons.append("incomplete_return_coverage")
            represented_sessions = len({r.session_date for r in rows})
            if represented_sessions < len(window_dates):
                reasons.append("no_sample_days_not_verified_zero")
            if any(r.coverage < 1 for r in rows):
                reasons.append("incomplete_source_coverage")
            score = (
                fsum(
                    (11 - r.rank) / 55
                    * min(max(float(r.interval_return or 0.0), 0.0), 0.3)
                    for r in values
                )
                if values
                else None
            )
            heat.append(FactorHeat(
                factor=factor, window_sessions=window, available_sessions=len(window_dates),
                represented_sessions=represented_sessions, event_count=len(rows),
                count=len(values), missing_count=sum(
                    r.matured_at is not None and r.interval_return is None for r in rows
                ), unmatured_count=sum(r.matured_at is None for r in rows),
                score=score,
                daily_mean_score=_ratio(score, len(window_dates)) if score is not None else None,
                coverage=_ratio(len(values), len(rows)),
                source_coverage=_mean([r.coverage for r in rows]),
                reasons=tuple(reasons),
            ))
    return EventEdgeReport(
        decision_session=decision_session,
        freeze_at=freeze_at,
        trading_sessions=sessions,
        baseline_dates=baseline_dates,
        cost_approvals=tuple(sorted(approvals, key=lambda a: a.cost_model_id)),
        event_records=events,
        top10_records=discovery,
        event_duplicates_removed=event_duplicates,
        top10_duplicates_removed=top_duplicates,
        overall=overall,
        factors=factors,
        heat=tuple(heat),
    )
