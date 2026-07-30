from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kernel.cross_asset_sentiment import (
    CrossAssetSentimentEngine,
    CrossAssetSentimentPolicy,
    PerpObservation,
    ProxyBinding,
    SentimentScope,
)

ASOF = datetime(2026, 7, 30, 1, 0, tzinfo=UTC)


def _observation(
    *,
    price: float,
    open_interest: float,
    observed_at_utc: datetime = ASOF,
) -> PerpObservation:
    return PerpObservation(
        venue="hyperliquid",
        market="main",
        instrument="BTC",
        observed_at_utc=observed_at_utc,
        mark_price=price,
        oracle_price=price / 1.001,
        open_interest=open_interest,
        funding_rate=0.00005,
        notional_volume_24h=1_000_000_000,
        bid_price=price - 0.5,
        ask_price=price + 0.5,
        aggressor_imbalance=0.6,
        long_liquidation_usd=100,
        short_liquidation_usd=1_000,
        active=True,
        provenance=f"hyperliquid.info@{observed_at_utc.isoformat()}",
    )


def test_fresh_long_building_scores_above_short_covering() -> None:
    binding = ProxyBinding(
        target_id="global-risk",
        scope=SentimentScope.MARKET,
        venue="hyperliquid",
        market="main",
        instrument="BTC",
        weight=1.0,
        min_notional_volume_24h=1_000_000,
    )
    engine = CrossAssetSentimentEngine(
        policy=CrossAssetSentimentPolicy(),
        bindings=(binding,),
    )
    previous = (_observation(price=100.0, open_interest=1_000),)

    fresh_longs = engine.evaluate(
        observations=(_observation(price=102.0, open_interest=1_100),),
        previous_observations=previous,
        asof_utc=ASOF + timedelta(seconds=1),
    )
    short_covering = engine.evaluate(
        observations=(_observation(price=102.0, open_interest=900),),
        previous_observations=previous,
        asof_utc=ASOF + timedelta(seconds=1),
    )

    assert fresh_longs.instrument_assessments[0].price_oi_regime == "fresh_longs"
    assert short_covering.instrument_assessments[0].price_oi_regime == "short_covering"
    fresh_score = fresh_longs.target_assessments[0].score
    covering_score = short_covering.target_assessments[0].score
    assert fresh_score is not None
    assert covering_score is not None
    assert fresh_score > covering_score
    assert fresh_longs.production_eligible is False


def test_stale_observation_degrades_to_unavailable() -> None:
    binding = ProxyBinding(
        target_id="global-risk",
        scope=SentimentScope.MARKET,
        venue="hyperliquid",
        market="main",
        instrument="BTC",
        weight=1.0,
        min_notional_volume_24h=1_000_000,
    )
    result = CrossAssetSentimentEngine(
        policy=CrossAssetSentimentPolicy(max_age_seconds=30),
        bindings=(binding,),
    ).evaluate(
        observations=(
            _observation(
                price=102,
                open_interest=1_100,
                observed_at_utc=ASOF - timedelta(minutes=2),
            ),
        ),
        asof_utc=ASOF,
    )

    assert result.instrument_assessments[0].score is None
    assert result.instrument_assessments[0].quality_reasons == (
        "stale_observation",
    )
    assert result.target_assessments[0].regime == "unavailable"
    assert result.target_assessments[0].confidence == 0


def test_raw_volume_does_not_supply_trade_direction() -> None:
    binding = ProxyBinding(
        target_id="global-risk",
        scope=SentimentScope.MARKET,
        venue="hyperliquid",
        market="main",
        instrument="BTC",
        weight=1.0,
        min_notional_volume_24h=1_000_000,
    )
    engine = CrossAssetSentimentEngine(
        policy=CrossAssetSentimentPolicy(),
        bindings=(binding,),
    )
    current = _observation(price=102, open_interest=1_100).model_copy(
        update={
            "aggressor_imbalance": None,
            "long_liquidation_usd": None,
            "short_liquidation_usd": None,
        }
    )
    previous = (_observation(price=100, open_interest=1_000),)

    low_volume = engine.evaluate(
        observations=(
            current.model_copy(update={"notional_volume_24h": 2_000_000}),
        ),
        previous_observations=previous,
        asof_utc=ASOF + timedelta(seconds=1),
    )
    high_volume = engine.evaluate(
        observations=(
            current.model_copy(
                update={"notional_volume_24h": 2_000_000_000}
            ),
        ),
        previous_observations=previous,
        asof_utc=ASOF + timedelta(seconds=1),
    )

    assert (
        low_volume.instrument_assessments[0].component_scores["signed_flow"]
        is None
    )
    assert (
        low_volume.target_assessments[0].score
        == high_volume.target_assessments[0].score
    )


def test_cross_venue_disagreement_reduces_confidence() -> None:
    bindings = (
        ProxyBinding(
            target_id="global-risk",
            scope=SentimentScope.MARKET,
            venue="hyperliquid",
            market="main",
            instrument="BTC",
            weight=0.5,
        ),
        ProxyBinding(
            target_id="global-risk",
            scope=SentimentScope.MARKET,
            venue="aevo",
            market="mainnet",
            instrument="BTC-PERP",
            weight=0.5,
        ),
    )
    engine = CrossAssetSentimentEngine(
        policy=CrossAssetSentimentPolicy(),
        bindings=bindings,
    )
    prior_hyperliquid = _observation(price=100, open_interest=1_000)
    prior_aevo = prior_hyperliquid.model_copy(
        update={
            "venue": "aevo",
            "market": "mainnet",
            "instrument": "btc-perp",
            "provenance": f"aevo.public@{ASOF.isoformat()}",
        }
    )
    bullish_hyperliquid = _observation(price=102, open_interest=1_100)
    bullish_aevo = prior_aevo.model_copy(
        update={
            "mark_price": 102,
            "oracle_price": 101.9,
            "open_interest": 1_100,
        }
    )
    bearish_aevo = bullish_aevo.model_copy(
        update={
            "mark_price": 98,
            "oracle_price": 98.1,
            "open_interest": 1_100,
            "funding_rate": -0.00005,
            "aggressor_imbalance": -0.6,
            "long_liquidation_usd": 1_000,
            "short_liquidation_usd": 100,
        }
    )
    previous = (prior_hyperliquid, prior_aevo)

    aligned = engine.evaluate(
        observations=(bullish_hyperliquid, bullish_aevo),
        previous_observations=previous,
        asof_utc=ASOF + timedelta(seconds=1),
    )
    conflicted = engine.evaluate(
        observations=(bullish_hyperliquid, bearish_aevo),
        previous_observations=previous,
        asof_utc=ASOF + timedelta(seconds=1),
    )

    assert (
        conflicted.target_assessments[0].confidence
        < aligned.target_assessments[0].confidence
    )
    assert conflicted.target_assessments[0].disagreement is not None
    assert conflicted.target_assessments[0].disagreement > 0


def test_small_price_and_open_interest_changes_cannot_flip_risk_regime() -> None:
    binding = ProxyBinding(
        target_id="global-risk",
        scope=SentimentScope.MARKET,
        venue="hyperliquid",
        market="main",
        instrument="BTC",
        weight=1.0,
    )
    engine = CrossAssetSentimentEngine(
        policy=CrossAssetSentimentPolicy(),
        bindings=(binding,),
    )
    previous = (_observation(price=100, open_interest=1_000),)
    current = _observation(price=99.92, open_interest=1_002).model_copy(
        update={
            "funding_rate": 0.0000125,
            "aggressor_imbalance": None,
            "long_liquidation_usd": None,
            "short_liquidation_usd": None,
        }
    )

    result = engine.evaluate(
        observations=(current,),
        previous_observations=previous,
        asof_utc=ASOF + timedelta(seconds=1),
    )

    assert result.target_assessments[0].regime == "neutral"
    assert result.target_assessments[0].score is not None
    assert result.target_assessments[0].score > -25
