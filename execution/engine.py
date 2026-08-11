"""Fail-closed orchestration from TradePlan to an idempotent Paper order."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from execution.alpaca_paper import BrokerOrder, PaperOrderRequest, PaperPosition
from execution.ledger import OrderLedger
from execution.order_state import OrderLifecycle, OrderState
from execution.reconcile import ReconciliationError, reconcile_broker_order
from kernel.config import Config
from kernel.guardrails import GuardrailContext, GuardrailVerdict, arbitrate_trade
from kernel.tradeplan import TradePlan


class PaperBroker(Protocol):
    writes_enabled: bool

    def submit_order_idempotent(self, request: PaperOrderRequest) -> BrokerOrder: ...

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...

    def list_positions(self) -> tuple[PaperPosition, ...]: ...


@dataclass(frozen=True)
class PaperExecutionResult:
    lifecycle: OrderLifecycle
    verdict: GuardrailVerdict
    broker_order_id: str | None
    dry_run: bool
    replayed: bool
    filled_avg_price: str | None = None
    position_confirmed: bool = False
    broker_identity: str = "unknown"


def _price(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


class PaperExecutionEngine:
    def __init__(
        self,
        *,
        broker: PaperBroker,
        ledger: OrderLedger,
        config: Config,
        paper_authorized: bool = False,
    ):
        self.broker = broker
        self.ledger = ledger
        self.config = config
        self.paper_authorized = paper_authorized

    def execute(self, plan: TradePlan, context: GuardrailContext) -> PaperExecutionResult:
        self.ledger.record_plan(plan)
        intent = OrderLifecycle(
            client_order_id=plan.client_order_id,
            plan_id=plan.plan_id,
            symbol=plan.symbol,
            requested_shares=plan.quantity,
        )
        lifecycle = self.ledger.create(intent, created_at_utc=plan.created_at_utc)
        if lifecycle.state in {
            OrderState.SUBMITTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_PENDING,
            OrderState.CANCELLED,
            OrderState.REJECTED,
        }:
            verdict = arbitrate_trade(plan, context, self.config)
            if lifecycle.state in {OrderState.CANCELLED, OrderState.REJECTED}:
                return PaperExecutionResult(
                    lifecycle=lifecycle,
                    verdict=verdict,
                    broker_order_id=self.ledger.get_broker_order_id(plan.client_order_id),
                    dry_run=lifecycle.state is OrderState.CANCELLED,
                    replayed=True,
                    broker_identity=self._broker_identity,
                )
            broker_order = self.broker.get_order_by_client_id(plan.client_order_id)
            if broker_order is None:
                raise ReconciliationError("local active order is missing from broker during replay")
            lifecycle = reconcile_broker_order(
                self.ledger,
                lifecycle,
                broker_order,
                at_utc=datetime.now(UTC),
            )
            filled_avg_price, position_confirmed = self._confirm_fill(lifecycle, broker_order)
            return PaperExecutionResult(
                lifecycle=lifecycle,
                verdict=verdict,
                broker_order_id=broker_order.id,
                dry_run=lifecycle.state is OrderState.CANCELLED,
                replayed=True,
                filled_avg_price=filled_avg_price,
                position_confirmed=position_confirmed,
                broker_identity=self._broker_identity,
            )

        now = datetime.now(UTC)
        if lifecycle.state is OrderState.CREATED:
            lifecycle = self.ledger.transition(
                plan.client_order_id,
                OrderState.PENDING_RISK,
                at_utc=now,
                provenance="execution.engine.pending_risk.v1",
            )

        verdict = arbitrate_trade(plan, context, self.config)
        if not verdict.approved:
            lifecycle = self.ledger.transition(
                plan.client_order_id,
                OrderState.REJECTED,
                at_utc=datetime.now(UTC),
                provenance=f"execution.engine.guardrail.{verdict.failure_code}",
            )
            return PaperExecutionResult(lifecycle, verdict, None, False, False)

        if lifecycle.state is OrderState.PENDING_RISK:
            lifecycle = self.ledger.transition(
                plan.client_order_id,
                OrderState.APPROVED,
                at_utc=datetime.now(UTC),
                provenance="execution.engine.guardrails_approved.v1",
            )

        if not self.broker.writes_enabled or not self.paper_authorized:
            lifecycle = self.ledger.transition(
                plan.client_order_id,
                OrderState.CANCELLED,
                at_utc=datetime.now(UTC),
                provenance="execution.engine.dry_run_not_authorized.v1",
            )
            return PaperExecutionResult(lifecycle, verdict, None, True, False)

        request = PaperOrderRequest(
            client_order_id=plan.client_order_id,
            symbol=plan.symbol,
            qty=plan.quantity,
            order_type=plan.entry_order_type,
            limit_price=_price(plan.entry_limit_price),
            take_profit_price=_price(plan.take_profit_price) or "",
            stop_loss_price=_price(plan.stop_loss_price) or "",
        )
        broker_order = self.broker.submit_order_idempotent(request)
        self.ledger.record_broker_order_id(plan.client_order_id, broker_order.id)
        lifecycle = self.ledger.transition(
            plan.client_order_id,
            OrderState.SUBMITTED,
            at_utc=datetime.now(UTC),
            provenance=f"alpaca.paper.order:{broker_order.id}",
        )
        lifecycle = reconcile_broker_order(
            self.ledger,
            lifecycle,
            broker_order,
            at_utc=datetime.now(UTC),
        )
        filled_avg_price, position_confirmed = self._confirm_fill(lifecycle, broker_order)
        return PaperExecutionResult(
            lifecycle=lifecycle,
            verdict=verdict,
            broker_order_id=broker_order.id,
            dry_run=False,
            replayed=False,
            filled_avg_price=filled_avg_price,
            position_confirmed=position_confirmed,
            broker_identity=self._broker_identity,
        )

    @property
    def _broker_identity(self) -> str:
        return str(getattr(self.broker, "broker_identity", "unknown"))

    def _confirm_fill(
        self,
        lifecycle: OrderLifecycle,
        broker_order: BrokerOrder,
    ) -> tuple[str | None, bool]:
        if lifecycle.state not in {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
        }:
            return None, False
        raw_price = broker_order.filled_avg_price
        if raw_price is None or not raw_price.strip():
            raise ReconciliationError("broker fill is missing average fill price confirmation")
        try:
            price = Decimal(raw_price)
        except InvalidOperation as exc:
            raise ReconciliationError("broker fill average price is invalid") from exc
        if not price.is_finite() or price <= 0:
            raise ReconciliationError("broker fill average price must be positive and finite")
        positions = self.broker.list_positions()
        matching = [position for position in positions if position.symbol == lifecycle.symbol]
        if len(matching) != 1:
            raise ReconciliationError("broker fill has no unique long position confirmation")
        position = matching[0]
        if position.side.strip().lower() != "long":
            raise ReconciliationError("broker fill position is not long")
        try:
            quantity = Decimal(position.qty)
        except InvalidOperation as exc:
            raise ReconciliationError("broker fill position quantity is invalid") from exc
        if not quantity.is_finite() or quantity < lifecycle.filled_shares:
            raise ReconciliationError("broker fill position quantity is not confirmed")
        return raw_price, True
