"""Causal, long-only cross-sectional momentum for liquid ETFs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import prod, sqrt


@dataclass(frozen=True)
class MomentumConfig:
    lookback_days: int = 252
    skip_days: int = 21
    holdings: int = 3
    max_weight: float = 0.40
    require_positive_momentum: bool = False

    def __post_init__(self) -> None:
        if self.lookback_days <= self.skip_days or self.skip_days < 0:
            raise ValueError("lookback_days must exceed nonnegative skip_days")
        if self.holdings <= 0 or not 0 < self.max_weight <= 1:
            raise ValueError("holdings and max_weight must be positive")


@dataclass(frozen=True)
class BacktestResult:
    dates: list[date]
    daily_returns: list[float]
    turnover: list[float]
    equity: list[float]


def month_end_indexes(dates: list[date]) -> set[int]:
    return {
        index
        for index, current in enumerate(dates)
        if index == len(dates) - 1
        or (dates[index + 1].year, dates[index + 1].month)
        != (current.year, current.month)
    }


def build_target_weights(
    dates: list[date],
    prices: dict[str, list[float]],
    config: MomentumConfig,
    *,
    rebalance_indexes: set[int] | None = None,
) -> list[dict[str, float]]:
    """Build close-time targets using only observations available at that close."""
    _validate_panel(dates, prices)
    symbols = sorted(prices)
    rebalances = month_end_indexes(dates) if rebalance_indexes is None else rebalance_indexes
    current = {symbol: 0.0 for symbol in symbols}
    output: list[dict[str, float]] = []
    for index in range(len(dates)):
        if index in rebalances and index >= config.lookback_days:
            scores = {
                symbol: prices[symbol][index - config.skip_days]
                / prices[symbol][index - config.lookback_days]
                - 1
                for symbol in symbols
            }
            eligible = [
                symbol
                for symbol in symbols
                if not config.require_positive_momentum or scores[symbol] > 0
            ]
            selected = sorted(eligible, key=lambda symbol: (-scores[symbol], symbol))[
                : config.holdings
            ]
            weight = min(1 / config.holdings, config.max_weight)
            current = {
                symbol: weight if symbol in selected else 0.0 for symbol in symbols
            }
        output.append(dict(current))
    return output


def backtest(
    dates: list[date],
    prices: dict[str, list[float]],
    targets: list[dict[str, float]],
    *,
    cost_bps: float,
) -> BacktestResult:
    """Apply close-time targets to the next close-to-close return (T+1)."""
    _validate_panel(dates, prices)
    if len(targets) != len(dates) or cost_bps < 0:
        raise ValueError("targets must align with dates and cost_bps must be nonnegative")
    symbols = sorted(prices)
    returns = [0.0] * len(dates)
    turnover = [0.0] * len(dates)
    equity = [1.0] * len(dates)
    previous = {symbol: 0.0 for symbol in symbols}
    for index in range(1, len(dates)):
        executed = targets[index - 1]
        changed = sum(abs(executed[symbol] - previous[symbol]) for symbol in symbols)
        gross = sum(
            executed[symbol]
            * (prices[symbol][index] / prices[symbol][index - 1] - 1)
            for symbol in symbols
        )
        returns[index] = gross - changed * cost_bps / 10_000
        turnover[index] = changed
        equity[index] = equity[index - 1] * (1 + returns[index])
        previous = executed
    return BacktestResult(dates, returns, turnover, equity)


def performance(result: BacktestResult) -> dict[str, float | int | None]:
    values = result.daily_returns
    if len(values) < 2:
        raise ValueError("at least two observations are required")
    years = max((result.dates[-1] - result.dates[0]).days / 365.25, 1 / 252)
    cagr = result.equity[-1] ** (1 / years) - 1
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / max(len(values) - 1, 1)
    peak = result.equity[0]
    max_drawdown = 0.0
    for value in result.equity:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1)
    gains = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    return {
        "observations": len(values),
        "total_return": result.equity[-1] - 1,
        "cagr": cagr,
        "annual_volatility": sqrt(variance * 252),
        "sharpe": mean / sqrt(variance) * sqrt(252) if variance > 0 else None,
        "max_drawdown": max_drawdown,
        "daily_profit_factor": gains / losses if losses else None,
        "annual_turnover": sum(result.turnover) / years,
        "positive_days": sum(value > 0 for value in values),
        "geometric_check": prod(1 + value for value in values) - 1,
    }


def slice_result(result: BacktestResult, start: date, end: date | None = None) -> BacktestResult:
    indexes = [
        index
        for index, value in enumerate(result.dates)
        if value >= start and (end is None or value <= end)
    ]
    if not indexes:
        raise ValueError("requested result slice is empty")
    daily = [result.daily_returns[index] for index in indexes]
    turnover = [result.turnover[index] for index in indexes]
    equity: list[float] = []
    value = 1.0
    for item in daily:
        value *= 1 + item
        equity.append(value)
    return BacktestResult([result.dates[index] for index in indexes], daily, turnover, equity)


def _validate_panel(dates: list[date], prices: dict[str, list[float]]) -> None:
    if not dates or not prices or dates != sorted(dates) or len(set(dates)) != len(dates):
        raise ValueError("dates and prices must form a nonempty ordered panel")
    if any(len(values) != len(dates) for values in prices.values()):
        raise ValueError("all price series must align with dates")
    if any(value <= 0 for values in prices.values() for value in values):
        raise ValueError("adjusted prices must be positive")
