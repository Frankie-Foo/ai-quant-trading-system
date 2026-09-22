"""Deterministic persistence weighting across intraday selection waves."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def _symbol(row: Mapping[str, object]) -> str:
    symbol = str(row.get("symbol", "")).strip().upper()
    if not symbol:
        raise ValueError("wave row requires symbol")
    return symbol


def _score(row: Mapping[str, object]) -> float:
    value = row.get("base_score")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("wave row requires finite base_score")
    try:
        score = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError("wave row requires finite base_score") from exc
    if not math.isfinite(score):
        raise ValueError("wave row requires finite base_score")
    return score


def rank_wave(
    rows: Iterable[Mapping[str, object]],
    *,
    prior_waves: Sequence[Iterable[Mapping[str, object]]],
    limit: int,
) -> list[dict[str, Any]]:
    """Rank current facts, adding one deterministic point per prior-wave appearance."""
    if limit < 1:
        raise ValueError("limit must be positive")
    prior_symbols = [
        {_symbol(row) for row in wave}
        for wave in prior_waves
    ]
    ranked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        symbol = _symbol(raw)
        if symbol in seen:
            raise ValueError("current wave symbols must be unique")
        seen.add(symbol)
        base_score = _score(raw)
        repeat_count = sum(symbol in wave for wave in prior_symbols)
        ranked.append(
            {
                **raw,
                "symbol": symbol,
                "base_score": base_score,
                "repeat_count": repeat_count,
                "weighted_score": base_score + repeat_count,
            }
        )
    return sorted(
        ranked,
        key=lambda row: (
            -float(row["weighted_score"]),
            -int(row["repeat_count"]),
            -float(row["base_score"]),
            str(row["symbol"]),
        ),
    )[:limit]


def rank_forward_pool(
    rows: Iterable[Mapping[str, object]],
    *,
    prior_waves: Sequence[Iterable[Mapping[str, object]]],
    limit: int,
) -> list[dict[str, Any]]:
    """Weight the existing forward rank without introducing a new alpha score."""
    materialized = [dict(row) for row in rows]
    if not materialized:
        return []
    forward_ranks: list[int] = []
    for row in materialized:
        value = row.get("forward_rank")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError("wave row requires positive forward_rank")
        try:
            rank = int(value)
        except (ValueError, OverflowError) as exc:
            raise ValueError("wave row requires positive forward_rank") from exc
        if rank < 1 or (isinstance(value, float) and value != rank):
            raise ValueError("wave row requires positive forward_rank")
        forward_ranks.append(rank)
    maximum_rank = max(forward_ranks)
    scored = [
        {**row, "base_score": float(maximum_rank + 1 - rank)}
        for row, rank in zip(materialized, forward_ranks, strict=True)
    ]
    return rank_wave(scored, prior_waves=prior_waves, limit=limit)
