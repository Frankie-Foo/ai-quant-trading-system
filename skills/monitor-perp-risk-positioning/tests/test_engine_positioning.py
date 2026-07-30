from __future__ import annotations

from datetime import UTC, datetime, timedelta

from perp_risk.config import load_config
from perp_risk.engine import SentimentEngine, TargetSignal
from perp_risk.models import PerpObservation, Regime, RiskSnapshot, Scope
from perp_risk.positioning import (
    PositionResolver,
    PositionState,
    recommend_position,
)

NOW = datetime(2026, 7, 30, 14, 0, tzinfo=UTC)


def _observations(
    *,
    current_price: float = 102,
    previous_price: float = 100,
    include_liquidations: bool,
) -> tuple[tuple[PerpObservation, ...], tuple[PerpObservation, ...]]:
    config = load_config()
    current = []
    previous = []
    for binding in config.bindings:
        volume = 100_000_000 if binding.min_notional_volume_24h is not None else None
        current.append(
            PerpObservation(
                venue=binding.venue,
                market=binding.market,
                instrument=binding.instrument,
                observed_at_utc=NOW,
                mark_price=current_price,
                oracle_price=current_price,
                open_interest=102,
                funding_rate=0.0001,
                notional_volume_24h=volume,
                bid_price=current_price - 0.01,
                ask_price=current_price + 0.01,
                aggressor_imbalance=1,
                aggressor_trade_count=10,
                long_liquidation_usd=(0 if include_liquidations else None),
                short_liquidation_usd=(100 if include_liquidations else None),
                liquidation_event_count=(1 if include_liquidations else None),
                provenance=f"current:{binding.observation_key}",
            )
        )
        previous.append(
            PerpObservation(
                venue=binding.venue,
                market=binding.market,
                instrument=binding.instrument,
                observed_at_utc=NOW - timedelta(seconds=60),
                mark_price=previous_price,
                oracle_price=previous_price,
                open_interest=100,
                funding_rate=0,
                notional_volume_24h=volume,
                bid_price=previous_price - 0.01,
                ask_price=previous_price + 0.01,
                aggressor_imbalance=0,
                aggressor_trade_count=10,
                provenance=f"previous:{binding.observation_key}",
            )
        )
    return tuple(current), tuple(previous)


def test_missing_liquidations_block_boost() -> None:
    config = load_config()
    current, previous = _observations(include_liquidations=False)

    signal = next(
        item
        for item in SentimentEngine(config).evaluate(
            observations=current,
            previous_observations=previous,
            asof_utc=NOW,
        )
        if item.target_id == "global-risk"
    )

    assert signal.score is not None and signal.score >= 60
    assert signal.candidate_multiplier == 1.0
    assert "boost_blocked_missing_liquidation" in signal.reasons


def test_complete_strong_evidence_creates_boost_candidate() -> None:
    config = load_config()
    current, previous = _observations(include_liquidations=True)

    signal = next(
        item
        for item in SentimentEngine(config).evaluate(
            observations=current,
            previous_observations=previous,
            asof_utc=NOW,
        )
        if item.target_id == "global-risk"
    )

    assert signal.candidate_multiplier == 1.2
    assert signal.boost_eligible is True
    assert signal.liquidation_coverage == 1


def test_single_source_sector_never_boosts() -> None:
    config = load_config()
    current, previous = _observations(include_liquidations=True)

    signal = next(
        item
        for item in SentimentEngine(config).evaluate(
            observations=current,
            previous_observations=previous,
            asof_utc=NOW,
        )
        if item.target_id == "energy-risk"
    )

    assert signal.candidate_multiplier == 1
    assert "boost_blocked_single_venue" in signal.reasons


def _signal(
    *,
    multiplier: float,
    regime: Regime,
    asof: datetime,
) -> TargetSignal:
    return TargetSignal(
        target_id="global-risk",
        scope=Scope.MARKET,
        regime=regime,
        score=-40 if regime is Regime.RISK_OFF else 70,
        confidence=1,
        coverage=1,
        liquidation_coverage=1,
        disagreement=0,
        available_sources=4,
        configured_sources=4,
        available_venues=2,
        venue_conflict=False,
        boost_eligible=multiplier > 1,
        candidate_multiplier=multiplier,
        reasons=(),
        sources=(),
        asof_utc=asof,
    )


def test_regular_risk_off_requires_two_independent_windows() -> None:
    config = load_config()
    resolver = PositionResolver(config.policy, window_seconds=60)

    first, state = resolver.resolve(
        _signal(multiplier=0.5, regime=Regime.RISK_OFF, asof=NOW),
        None,
    )
    duplicate, state = resolver.resolve(
        _signal(
            multiplier=0.5,
            regime=Regime.RISK_OFF,
            asof=NOW + timedelta(seconds=10),
        ),
        state,
    )
    second, _ = resolver.resolve(
        _signal(
            multiplier=0.5,
            regime=Regime.RISK_OFF,
            asof=NOW + timedelta(seconds=60),
        ),
        state,
    )

    assert first.effective_multiplier == 1
    assert duplicate.effective_multiplier == 1
    assert second.effective_multiplier == 0.5


def test_strong_risk_off_is_immediate_and_recovery_is_slow() -> None:
    config = load_config()
    resolver = PositionResolver(config.policy, window_seconds=60)
    prior = PositionState(
        target_id="global-risk",
        effective_multiplier=1,
        pending_multiplier=1,
        pending_windows=0,
        last_window_id=int(NOW.timestamp()) // 60 - 1,
    )

    defensive, state = resolver.resolve(
        _signal(multiplier=0, regime=Regime.RISK_OFF, asof=NOW),
        prior,
    )
    recovering, _ = resolver.resolve(
        _signal(
            multiplier=1,
            regime=Regime.NEUTRAL,
            asof=NOW + timedelta(seconds=60),
        ),
        state,
    )

    assert defensive.effective_multiplier == 0
    assert recovering.effective_multiplier == 0
    assert recovering.pending_windows == 1
    assert "confirmation_pending:1/2" in recovering.reasons
    snapshot = RiskSnapshot(
        skill_version="0.1.0",
        snapshot_id="recovery",
        asof_utc=NOW + timedelta(seconds=60),
        data_cutoff_utc=NOW + timedelta(seconds=60),
        config_hash="hash",
        actionable=True,
        session_state="actionable",
        provider_status=(),
        targets=(recovering,),
        production_eligible=False,
        execution_eligible=False,
        orders_submitted=0,
    )
    recommendation = recommend_position(
        snapshot,
        relevant_targets=("global-risk",),
    )
    assert "global-risk:confirmation_pending:1/2" in recommendation.reasons


def test_boost_requires_two_independent_windows() -> None:
    config = load_config()
    resolver = PositionResolver(config.policy, window_seconds=60)

    first, state = resolver.resolve(
        _signal(multiplier=1.2, regime=Regime.RISK_ON, asof=NOW),
        None,
    )
    second, _ = resolver.resolve(
        _signal(
            multiplier=1.2,
            regime=Regime.RISK_ON,
            asof=NOW + timedelta(seconds=60),
        ),
        state,
    )

    assert first.effective_multiplier == 1
    assert "confirmation_pending:1/2" in first.reasons
    assert second.effective_multiplier == 1.2
    assert "confirmation_pending:1/2" not in second.reasons


def test_recommendation_uses_risk_veto_and_never_executes() -> None:
    config = load_config()
    signals = (
        _signal(multiplier=1.2, regime=Regime.RISK_ON, asof=NOW),
        TargetSignal(
            **{
                **_signal(
                    multiplier=0.5,
                    regime=Regime.RISK_OFF,
                    asof=NOW,
                ).__dict__,
                "target_id": "semiconductor-risk",
            }
        ),
    )
    resolver = PositionResolver(config.policy, window_seconds=60)
    priors = {
        "global-risk": PositionState(
            target_id="global-risk",
            effective_multiplier=1.2,
            pending_multiplier=1.2,
            pending_windows=0,
            last_window_id=int(NOW.timestamp()) // 60 - 1,
        ),
        "semiconductor-risk": PositionState(
            target_id="semiconductor-risk",
            effective_multiplier=0.5,
            pending_multiplier=0.5,
            pending_windows=0,
            last_window_id=int(NOW.timestamp()) // 60 - 1,
        ),
    }
    targets = tuple(resolver.resolve(item, priors[item.target_id])[0] for item in signals)
    snapshot = RiskSnapshot(
        skill_version="0.1.0",
        snapshot_id="snapshot",
        asof_utc=NOW,
        data_cutoff_utc=NOW,
        config_hash="hash",
        actionable=True,
        session_state="actionable",
        provider_status=(),
        targets=targets,
        production_eligible=False,
        execution_eligible=False,
        orders_submitted=0,
    )

    result = recommend_position(
        snapshot,
        relevant_targets=("global-risk", "semiconductor-risk"),
        base_target_position_pct=10,
    )

    assert result.position_multiplier == 0.5
    assert result.adjusted_target_position_pct == 5
    assert result.orders_submitted == 0
    assert result.execution_eligible is False
