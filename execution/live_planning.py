"""Causal conversion from an online ORB intent and NBBO into a TradePlan."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from execution.alpaca_sip_stream import SipQuote
from execution.locked_selection import LockedCandidate
from kernel.config import Config
from kernel.exits import ExitPlan, make_exits
from kernel.signals import OrbIntent
from kernel.sizing import SizingResult, size_position
from kernel.tradeplan import TradePlan


@dataclass(frozen=True)
class PlannedTrade:
    plan: TradePlan
    sizing: SizingResult
    exits: ExitPlan


def _tick(price: Decimal) -> Decimal:
    return Decimal("0.01") if price >= 1 else Decimal("0.0001")


def build_live_trade_plan(
    candidate: LockedCandidate,
    intent: OrbIntent,
    quote: SipQuote,
    *,
    trade_date: date,
    selection_snapshot_id: str,
    account_equity: float,
    is_half_day: bool,
    created_at_utc: datetime,
    cfg: Config,
) -> PlannedTrade:
    if created_at_utc.tzinfo is None or created_at_utc.utcoffset() != timedelta(0):
        raise ValueError("created_at_utc must be timezone-aware UTC")
    if not intent.triggered or intent.trigger_ts_utc is None:
        raise ValueError("a triggered causal ORB intent is required")
    if intent.planned_entry_ts_utc is None:
        raise ValueError("ORB intent is missing its planned entry boundary")
    if quote.symbol != candidate.symbol or intent.symbol != candidate.symbol:
        raise ValueError("candidate, signal, and quote symbols must match")
    quote_age = (created_at_utc - quote.ts_utc).total_seconds()
    if quote_age < 0 or quote_age > cfg.market_data.max_quote_age_seconds:
        raise ValueError("NBBO quote is stale or from the future")
    if quote.bid_price <= 0 or quote.ask_price <= 0 or quote.ask_price < quote.bid_price:
        raise ValueError("NBBO quote is missing or crossed")
    reference = Decimal(str(quote.ask_price))
    atr14 = float(reference) * candidate.atr_pct
    sizing = size_position(
        symbol=candidate.symbol,
        price=float(reference),
        atr14=atr14,
        adv_usd=candidate.adv_usd,
        tier=candidate.tier,
        confidence=1.0,
        cfg=cfg,
        capital_override=account_equity,
    )
    if sizing.shares <= 0:
        raise ValueError("risk sizing produced zero shares")
    exits = make_exits(
        float(reference),
        atr14,
        trade_date=trade_date,
        is_half_day=is_half_day,
        cfg=cfg,
    )
    tick = _tick(reference)
    take_profit = Decimal(str(exits.tp_px)).quantize(tick, rounding=ROUND_DOWN)
    stop_loss = Decimal(str(exits.sl_px)).quantize(tick, rounding=ROUND_UP)
    if stop_loss <= 0 or not stop_loss < reference < take_profit:
        raise ValueError("rounded bracket prices are invalid")
    trigger_text = intent.trigger_ts_utc.strftime("%H%M%S")
    plan_id = f"orb5-{trade_date:%Y%m%d}-{candidate.symbol}-{trigger_text}"
    signal_id = f"sip.bar:{candidate.symbol}:{intent.trigger_ts_utc.isoformat()}"
    quote_id = f"sip.quote:{candidate.symbol}:{quote.ts_utc.isoformat()}"
    plan = TradePlan(
        plan_id=plan_id,
        trace_id=f"trace-{plan_id}",
        strategy_version="orb5.live.v1",
        symbol=candidate.symbol,
        trade_date=trade_date,
        decision_asof_utc=created_at_utc,
        created_at_utc=created_at_utc,
        quantity=sizing.shares,
        reference_price=reference,
        take_profit_price=take_profit,
        stop_loss_price=stop_loss,
        time_stop_utc=exits.time_stop_utc,
        source_snapshot_ids=(selection_snapshot_id, signal_id, quote_id),
        provenance=(
            f"{intent.provenance}|{quote.provenance}|{sizing.provenance}|"
            f"{exits.provenance}|reference=known_ask"
        ),
    )
    return PlannedTrade(plan=plan, sizing=sizing, exits=exits)
