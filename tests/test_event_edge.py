from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from research.event_edge import (
    CostApproval,
    EventRecord,
    Provenance,
    Top10Record,
    TradingSession,
    build_event_edge,
)


def _sessions(count: int) -> tuple[TradingSession, ...]:
    first = date(2026, 8, 31)
    return tuple(
        TradingSession(
            session_date=day,
            open_at=datetime(day.year, day.month, day.day, 13, 30, tzinfo=UTC),
            close_at=datetime(day.year, day.month, day.day, 20, 0, tzinfo=UTC),
        )
        for day in (first + timedelta(days=offset) for offset in range(count))
    )


PROVENANCE = Provenance(
    source_kind="research_replay",
    source_id="test.market",
    snapshot_id="snapshot-1",
    evidence_ids=("evidence-1",),
    quality="passed",
)


def _event(
    session_date: date,
    index: int,
    *,
    net_return: float | None,
    matured: bool = True,
    factor: str = "contract",
) -> EventRecord:
    start = datetime(
        session_date.year, session_date.month, session_date.day, 14, 0, tzinfo=UTC
    )
    return EventRecord(
        event_cluster_id=f"cluster-{index}",
        symbol=f"S{index}",
        session_date=session_date,
        factor=factor,
        observed_at=start,
        matured_at=start + timedelta(minutes=15) if matured else None,
        coverage=1.0,
        provenance=PROVENANCE,
        source="all_events",
        label_definition_id="event-response.v1",
        label_origin="event_response",
        horizon_minutes=15,
        label_start_at=start,
        cost_model_id="cost.v1",
        net_return=net_return,
        mfe=max(net_return or 0.0, 0.0) if matured and net_return is not None else None,
        mae=min(net_return or 0.0, 0.0) if matured and net_return is not None else None,
        missing_reason=None if net_return is not None else "outcome_unavailable",
    )


def _approval(freeze_at: datetime) -> CostApproval:
    return CostApproval(
        cost_model_id="cost.v1",
        approved_at=freeze_at - timedelta(hours=1),
        approved_by="owner",
        evidence_id="cost-evidence-1",
    )


def test_edge_statistics_keep_positive_negative_zero_missing_and_unmatured_samples() -> None:
    sessions = _sessions(7)
    freeze = sessions[-1].open_at
    dates = [session.session_date for session in sessions[:-1]]
    records = (
        _event(dates[0], 1, net_return=0.10),
        _event(dates[1], 2, net_return=-0.05),
        _event(dates[2], 3, net_return=0.0),
        _event(dates[3], 4, net_return=None),
        _event(dates[4], 5, net_return=None, matured=False),
    )

    report = build_event_edge(
        records=records,
        top10_records=(),
        trading_sessions=sessions,
        decision_session=sessions[-1].session_date,
        freeze_at=freeze,
        cost_approvals=(_approval(freeze),),
    )

    assert report.overall.total_count == 5
    assert (report.overall.count, report.overall.missing_count) == (3, 1)
    assert report.overall.unmatured_count == 1
    assert (report.overall.wins, report.overall.losses, report.overall.flat_count) == (
        1,
        1,
        1,
    )
    assert report.overall.mean == pytest.approx(1 / 60)
    assert report.overall.median == 0.0
    assert report.overall.avg_win == 0.10
    assert report.overall.avg_loss == -0.05
    assert report.overall.profit_factor == pytest.approx(2.0)
    assert report.overall.coverage == pytest.approx(0.6)
    assert report.overall.expectancy_ci95 is None
    assert report.promotion_eligible is False
    assert report.production_eligible is False


def test_exact_duplicates_are_removed_but_conflicting_sample_keys_are_rejected() -> None:
    sessions = _sessions(3)
    freeze = sessions[-1].open_at
    record = _event(sessions[0].session_date, 1, net_return=0.02)

    report = build_event_edge(
        records=(record, record),
        top10_records=(),
        trading_sessions=sessions,
        decision_session=sessions[-1].session_date,
        freeze_at=freeze,
        cost_approvals=(_approval(freeze),),
    )
    assert report.event_duplicates_removed == 1
    assert report.overall.count == 1

    with pytest.raises(ValueError, match="conflicting"):
        build_event_edge(
            records=(record, record.model_copy(update={"net_return": 0.03})),
            top10_records=(),
            trading_sessions=sessions,
            decision_session=sessions[-1].session_date,
            freeze_at=freeze,
            cost_approvals=(_approval(freeze),),
        )


def test_heat_uses_rank_weights_caps_gains_and_normalizes_by_calendar_days() -> None:
    sessions = _sessions(11)
    freeze = sessions[-1].open_at
    recent = sessions[-4:-1]
    returns = ((1, 0.40), (10, 0.10), (5, -0.20))
    top10 = tuple(
        Top10Record(
            event_cluster_id=f"heat-{rank}",
            symbol=f"H{rank}",
            session_date=session.session_date,
            factor="contract",
            observed_at=session.close_at,
            matured_at=session.close_at,
            coverage=1.0,
            provenance=PROVENANCE,
            source="postclose_top10",
            rank=rank,
            interval_return=value,
        )
        for session, (rank, value) in zip(recent, returns, strict=True)
    )

    report = build_event_edge(
        records=(),
        top10_records=top10,
        trading_sessions=sessions,
        decision_session=sessions[-1].session_date,
        freeze_at=freeze,
        cost_approvals=(),
    )
    heat = {item.window_sessions: item for item in report.heat}
    expected_score = 10 / 55 * 0.30 + 1 / 55 * 0.10

    assert heat[3].score == pytest.approx(expected_score)
    assert heat[3].daily_mean_score == pytest.approx(expected_score / 3)
    assert heat[5].daily_mean_score == pytest.approx(expected_score / 5)
    assert heat[10].daily_mean_score == pytest.approx(expected_score / 10)
    assert all(item.complete_market_estimate is False for item in heat.values())


def test_freeze_boundary_and_false_late_maturity_are_rejected() -> None:
    sessions = _sessions(3)
    freeze = sessions[-1].open_at
    pending = _event(sessions[0].session_date, 1, net_return=None, matured=False)
    future = pending.model_copy(update={"observed_at": freeze})
    with pytest.raises(ValueError, match="future or freeze-boundary"):
        build_event_edge(
            records=(future,),
            top10_records=(),
            trading_sessions=sessions,
            decision_session=sessions[-1].session_date,
            freeze_at=freeze,
            cost_approvals=(),
        )

    late_start = datetime.combine(
        sessions[0].session_date, datetime.min.time(), UTC
    ).replace(hour=18, minute=40)
    falsely_matured = _event(sessions[0].session_date, 2, net_return=0.05).model_copy(
        update={
            "horizon_minutes": 30,
            "label_start_at": late_start,
            "matured_at": late_start + timedelta(minutes=30),
        }
    )
    with pytest.raises(ValueError, match="complete origin-specific horizon"):
        build_event_edge(
            records=(falsely_matured,),
            top10_records=(),
            trading_sessions=sessions,
            decision_session=sessions[-1].session_date,
            freeze_at=freeze,
            cost_approvals=(),
        )


def test_public_models_reject_extra_fields_and_non_utc_timestamps() -> None:
    with pytest.raises(ValidationError):
        Provenance.model_validate({**PROVENANCE.model_dump(), "unexpected": "value"})
    with pytest.raises(ValidationError):
        CostApproval(
            cost_model_id="cost.v1",
            approved_at=datetime(2026, 9, 14, 10, 0),
            approved_by="owner",
            evidence_id="evidence",
        )
