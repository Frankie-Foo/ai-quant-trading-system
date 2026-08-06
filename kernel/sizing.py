"""Risk, liquidity, and market-cap tier position caps."""

from __future__ import annotations

import math
from dataclasses import dataclass

from kernel.config import Config


@dataclass(frozen=True)
class SizingResult:
    symbol: str
    capital_base: float
    risk_cap: float
    liquidity_cap: float
    tier_cap: float
    final_notional: float
    shares: int
    binding_cap: str
    provenance: str


def size_position(
    symbol: str,
    price: float,
    atr14: float,
    adv_usd: float,
    tier: str,
    confidence: float,
    cfg: Config,
    *,
    execution_window_fraction: float = 1.0,
    capital_override: float | None = None,
) -> SizingResult:
    values = (price, atr14, adv_usd, confidence, execution_window_fraction)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("sizing inputs must be finite")
    if price <= 0 or atr14 <= 0 or adv_usd <= 0:
        raise ValueError("price, ATR, and ADV must be positive")
    if not 0 < confidence <= 1:
        raise ValueError("confidence must be in (0, 1]")
    if not 0 < execution_window_fraction <= 1:
        raise ValueError("execution_window_fraction must be in (0, 1]")
    if capital_override is not None and (
        not math.isfinite(capital_override) or capital_override <= 0
    ):
        raise ValueError("capital_override must be finite and positive")
    tier_config = getattr(cfg.tiers, tier, None)
    if tier_config is None:
        raise ValueError(f"unknown market-cap tier: {tier}")

    stop_distance_pct = atr14 / price
    capital_base = (
        cfg.capital if capital_override is None else min(cfg.capital, capital_override)
    )
    risk_cap = cfg.risk_per_trade * capital_base / stop_distance_pct
    liquidity_cap = cfg.participation_cap * adv_usd * execution_window_fraction
    tier_cap = tier_config.weight * capital_base
    caps = {
        "risk_cap": risk_cap,
        "liquidity_cap": liquidity_cap,
        "tier_cap": tier_cap,
    }
    binding_cap = min(caps, key=caps.__getitem__)
    final_notional = caps[binding_cap] * confidence
    shares = math.floor(final_notional / price)
    return SizingResult(
        symbol=symbol,
        capital_base=capital_base,
        risk_cap=risk_cap,
        liquidity_cap=liquidity_cap,
        tier_cap=tier_cap,
        final_notional=final_notional,
        shares=shares,
        binding_cap=binding_cap,
        provenance=(
            f"kernel.sizing.size_position|capital_base={capital_base}|"
            f"configured_capital={cfg.capital}|"
            f"risk={cfg.risk_per_trade}|participation={cfg.participation_cap}|"
            f"confidence={confidence}"
        ),
    )
