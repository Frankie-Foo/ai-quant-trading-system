"""Stateful causal SIP -> ORB intent -> TradePlan -> Paper guardrail loop."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

from execution.alpaca_paper import PaperAccount, PaperPosition
from execution.alpaca_sip_stream import SipBar, SipEvent, SipQuote
from execution.engine import PaperExecutionEngine, PaperExecutionResult
from execution.live_planning import build_live_trade_plan
from execution.locked_selection import LockedCandidate, LockedSelection
from execution.sip_store import SipEventStore
from kernel.config import Config
from kernel.guardrails import GuardrailContext
from kernel.signals import OrbIntent, orb5_intent


class SessionBroker(Protocol):
    def get_account(self) -> PaperAccount: ...

    def list_positions(self) -> tuple[PaperPosition, ...]: ...


def _decimal(value: str, *, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"broker {name} is invalid") from exc
    if not parsed.is_finite():
        raise ValueError(f"broker {name} is not finite")
    return parsed


class LiveSessionProcessor:
    def __init__(
        self,
        *,
        selection: LockedSelection,
        session_open_utc: datetime,
        session_close_utc: datetime,
        is_half_day: bool,
        store: SipEventStore,
        engine: PaperExecutionEngine,
        broker: SessionBroker,
        config: Config,
        kill_switch_active: bool,
    ):
        self.selection = selection
        self.session_open_utc = session_open_utc
        self.session_close_utc = session_close_utc
        self.is_half_day = is_half_day
        self.store = store
        self.engine = engine
        self.broker = broker
        self.config = config
        self.kill_switch_active = kill_switch_active
        self._candidates = {candidate.symbol: candidate for candidate in selection.candidates}
        self._pending: dict[str, OrbIntent] = {}
        self._attempted: set[str] = set()

    @property
    def attempted_symbols(self) -> frozenset[str]:
        return frozenset(self._attempted)

    def process(
        self,
        event: SipEvent,
        *,
        received_at_utc: datetime,
    ) -> PaperExecutionResult | None:
        if received_at_utc.tzinfo is None or received_at_utc.utcoffset() != timedelta(0):
            raise ValueError("received_at_utc must be timezone-aware UTC")
        self.store.append(event)
        if event.symbol in self._attempted or event.symbol not in self._candidates:
            return None
        if isinstance(event, SipBar):
            decision_at = event.ts_utc + timedelta(minutes=1)
            if not self.session_open_utc < decision_at < self.session_close_utc:
                return None
            bars = self.store.bars_for_symbol(
                event.symbol,
                start_utc=self.session_open_utc,
                end_utc=decision_at,
            )
            candidate = self._candidates[event.symbol]
            intent = orb5_intent(
                bars,
                session_open_utc=self.session_open_utc,
                decision_at_utc=decision_at,
                rvol=candidate.rvol,
                min_rvol=self.config.universe.min_rvol,
            )
            if intent.triggered:
                self._pending[event.symbol] = intent
                latest = self.store.latest_quote(event.symbol)
                if latest is not None and latest.ts_utc >= decision_at:
                    return self._execute(candidate, intent, latest, received_at_utc)
            return None
        pending_intent = self._pending.get(event.symbol)
        if pending_intent is None or pending_intent.planned_entry_ts_utc is None:
            return None
        if event.ts_utc < pending_intent.planned_entry_ts_utc:
            return None
        return self._execute(
            self._candidates[event.symbol], pending_intent, event, received_at_utc
        )

    def _execute(
        self,
        candidate: LockedCandidate,
        intent: OrbIntent,
        quote: SipQuote,
        received_at_utc: datetime,
    ) -> PaperExecutionResult:
        account = self.broker.get_account()
        positions = self.broker.list_positions()
        equity = _decimal(account.equity, name="equity")
        last_equity = _decimal(account.last_equity, name="last_equity")
        buying_power = _decimal(account.buying_power, name="buying_power")
        gross_exposure = sum(
            (
                abs(_decimal(position.market_value, name="position market value"))
                for position in positions
            ),
            start=Decimal("0"),
        )
        planned = build_live_trade_plan(
            candidate,
            intent,
            quote,
            trade_date=self.selection.trade_date,
            selection_snapshot_id=self.selection.snapshot.dataset_id,
            account_equity=float(equity),
            is_half_day=self.is_half_day,
            created_at_utc=received_at_utc,
            cfg=self.config,
        )
        context = GuardrailContext(
            evaluated_at_utc=received_at_utc,
            market_data_asof_utc=quote.ts_utc,
            market_data_feed="sip",
            paper_endpoint=True,
            kill_switch_active=self.kill_switch_active,
            market_open=self.session_open_utc <= received_at_utc < self.session_close_utc,
            account_active=account.status.upper() == "ACTIVE",
            account_blocked=account.account_blocked,
            trading_blocked=account.trading_blocked,
            equity=equity,
            daily_pnl=equity - last_equity,
            gross_exposure=gross_exposure,
            open_position_symbols=tuple(position.symbol for position in positions),
            buying_power=buying_power,
            sizing_notional_cap=Decimal(str(planned.sizing.final_notional)),
            selected_symbols=self.selection.symbols,
            selection_snapshot_ids=(self.selection.snapshot.dataset_id,),
        )
        result = self.engine.execute(planned.plan, context)
        self._attempted.add(candidate.symbol)
        self._pending.pop(candidate.symbol, None)
        return result
