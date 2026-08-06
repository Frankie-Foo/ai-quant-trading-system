"""Bidirectional post-trade attribution that separates process from luck."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class DecisionQuality(StrEnum):
    DISCIPLINED_WIN = "disciplined_win"
    DISCIPLINED_LOSS = "disciplined_loss"
    LUCKY_WIN = "lucky_win"
    AVOIDABLE_LOSS = "avoidable_loss"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class DecisionOutcomeFacts:
    symbol: str
    rules_compliant: bool | None
    net_return: float | None
    selection_facts: tuple[str, ...]
    entry_facts: tuple[str, ...]
    exit_facts: tuple[str, ...]
    violation_facts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("decision-review symbol must be normalized uppercase")
        if self.net_return is not None and not math.isfinite(self.net_return):
            raise ValueError("decision-review net return must be finite")
        for group in (
            self.selection_facts,
            self.entry_facts,
            self.exit_facts,
            self.violation_facts,
        ):
            if any(not fact.strip() for fact in group):
                raise ValueError("decision-review facts cannot be blank")


@dataclass(frozen=True)
class DecisionQualityReview:
    symbol: str
    quality: DecisionQuality
    net_return: float | None
    profit_reasons: tuple[str, ...]
    loss_reasons: tuple[str, ...]
    violations: tuple[str, ...]
    production_change_allowed: bool
    provenance: str


def build_bidirectional_review(
    facts: tuple[DecisionOutcomeFacts, ...],
) -> tuple[DecisionQualityReview, ...]:
    symbols = [item.symbol for item in facts]
    if len(symbols) != len(set(symbols)):
        raise ValueError("decision review contains duplicate symbols")
    rows: list[DecisionQualityReview] = []
    for item in facts:
        evidence = _unique(
            item.selection_facts + item.entry_facts + item.exit_facts
        )
        quality = _quality(item.rules_compliant, item.net_return)
        rows.append(
            DecisionQualityReview(
                symbol=item.symbol,
                quality=quality,
                net_return=item.net_return,
                profit_reasons=(
                    evidence
                    if item.net_return is not None and item.net_return > 0
                    else ()
                ),
                loss_reasons=(
                    evidence
                    if item.net_return is not None and item.net_return < 0
                    else ()
                ),
                violations=_unique(item.violation_facts),
                production_change_allowed=False,
                provenance="research.decision_quality.bidirectional.v1",
            )
        )
    return tuple(rows)


def _quality(
    rules_compliant: bool | None,
    net_return: float | None,
) -> DecisionQuality:
    if rules_compliant is None or net_return is None or net_return == 0:
        return DecisionQuality.UNCLASSIFIED
    if rules_compliant and net_return > 0:
        return DecisionQuality.DISCIPLINED_WIN
    if rules_compliant:
        return DecisionQuality.DISCIPLINED_LOSS
    if net_return > 0:
        return DecisionQuality.LUCKY_WIN
    return DecisionQuality.AVOIDABLE_LOSS


@dataclass(frozen=True)
class TailShadowFacts:
    symbol: str
    entry_price: float
    main_exit_price: float
    standard_tail_exit_price: float
    high_tail_exit_price: float
    a_plus_plus_tail_exit_price: float

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("tail-shadow symbol must be normalized uppercase")
        prices = (
            self.entry_price,
            self.main_exit_price,
            self.standard_tail_exit_price,
            self.high_tail_exit_price,
            self.a_plus_plus_tail_exit_price,
        )
        if any(not math.isfinite(price) or price <= 0 for price in prices):
            raise ValueError("tail-shadow prices must be finite and positive")


@dataclass(frozen=True)
class TailShadowReview:
    symbol: str
    no_tail_return: float
    standard_20_return: float
    high_25_return: float
    a_plus_plus_30_return: float
    best_shadow: str
    production_change_allowed: bool
    provenance: str


def build_tail_shadow(facts: TailShadowFacts) -> TailShadowReview:
    main = facts.main_exit_price / facts.entry_price - 1
    standard_tail = facts.standard_tail_exit_price / facts.entry_price - 1
    high_tail = facts.high_tail_exit_price / facts.entry_price - 1
    a_plus_plus_tail = facts.a_plus_plus_tail_exit_price / facts.entry_price - 1
    values = {
        "no_tail": _rounded(main),
        "standard_20": _rounded(0.80 * main + 0.20 * standard_tail),
        "high_25": _rounded(0.75 * main + 0.25 * high_tail),
        "a_plus_plus_30": _rounded(0.70 * main + 0.30 * a_plus_plus_tail),
    }
    return TailShadowReview(
        symbol=facts.symbol,
        no_tail_return=values["no_tail"],
        standard_20_return=values["standard_20"],
        high_25_return=values["high_25"],
        a_plus_plus_30_return=values["a_plus_plus_30"],
        best_shadow=max(values, key=values.__getitem__),
        production_change_allowed=False,
        provenance="research.decision_quality.tail_shadow.v1",
    )


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _rounded(value: float) -> float:
    return round(value, 8)
