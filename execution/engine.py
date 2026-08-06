"""Fail-closed orchestration from TradePlan to an idempotent Paper order."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from execution.alpaca_paper import BrokerOrder, PaperOrderRequest
from execution.ledger import OrderLedger
from execution.order_state import OrderLifecycle, OrderState
from execution.reconcile import reconcile_broker_order
from kernel.config import Config
from kernel.guardrails import GuardrailContext, GuardrailVerdict, arbitrate_trade
from kernel.tradeplan import TradePlan


class PaperBroker(Protocol):
    writes_enabled: bool

    def submit_order_idempotent(self, request: PaperOrderRequest) -> BrokerOrder: ...


@dataclass(frozen=True)
class PaperExecutionResult:
    lifecycle: OrderLifecycle
    verdict: GuardrailVerdict
    broker_order_id: str | None
    dry_run: bool
    replayed: bool


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
            return PaperExecutionResult(
                lifecycle=lifecycle,
                verdict=verdict,
                broker_order_id=self.ledger.get_broker_order_id(plan.client_order_id),
                dry_run=lifecycle.state is OrderState.CANCELLED,
                replayed=True,
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
        return PaperExecutionResult(
            lifecycle=lifecycle,
            verdict=verdict,
            broker_order_id=broker_order.id,
            dry_run=False,
            replayed=False,
        )
