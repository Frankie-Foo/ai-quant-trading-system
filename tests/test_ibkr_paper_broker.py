from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest

from execution.alpaca_paper import (
    BrokerWritesDisabledError,
    PaperCloseRequest,
    PaperOrderRequest,
)
from execution.ibkr_execution import (
    PAPER_PORT,
    BrokerAccountSnapshot,
    BrokerSubmission,
    BrokerWhatIf,
)
from execution.ibkr_paper_broker import IBKRPaperBroker
from execution.ibkr_tws_adapter import IbkrAccountValues, IbkrOrderCommand


@dataclass
class FakePaperTransport:
    connected: bool = False
    connected_to: tuple[str, int, int] | None = None
    submissions: list[tuple[IbkrOrderCommand, ...]] = field(default_factory=list)
    cancelled: list[int] = field(default_factory=list)
    what_if_warning: str | None = None
    known: dict[str, BrokerSubmission] = field(default_factory=dict)

    def connect(self, *, host: str, port: int, client_id: int) -> None:
        self.connected = True
        self.connected_to = (host, port, client_id)

    def disconnect(self) -> None:
        self.connected = False

    def account_snapshot(self) -> BrokerAccountSnapshot:
        return BrokerAccountSnapshot(
            account_id="DU7654321",
            api_read_only=False,
            positions=(
                {
                    "account_id": "DU7654321",
                    "symbol": "AAPL",
                    "security_type": "STK",
                    "quantity": "3",
                    "average_cost": "190",
                },
            ),
            open_orders=(),
        )

    def account_values(self) -> IbkrAccountValues:
        return IbkrAccountValues(
            account_id="DU7654321",
            net_liquidation=Decimal("100000"),
            previous_equity=Decimal("101000"),
            buying_power=Decimal("400000"),
        )

    def what_if_order_command(self, command: IbkrOrderCommand) -> BrokerWhatIf:
        del command
        return BrokerWhatIf(
            accepted=True,
            estimated_commission=Decimal("1.00"),
            initial_margin_change=Decimal("200"),
            warning=self.what_if_warning,
        )

    def submit_order_group(
        self, commands: tuple[IbkrOrderCommand, ...]
    ) -> tuple[BrokerSubmission, ...]:
        self.submissions.append(commands)
        base = len(self.submissions) * 100
        result = tuple(
            BrokerSubmission(
                status="submitted",
                order_id=base + index,
                perm_id=None,
                order_ref=command.order_ref,
            )
            for index, command in enumerate(commands)
        )
        self.known.update({item.order_ref: item for item in result})
        return result

    def find_by_order_ref(self, order_ref: str) -> BrokerSubmission | None:
        return self.known.get(order_ref)

    def cancel_order(self, order_id: int) -> bool:
        self.cancelled.append(order_id)
        return True


def _broker(
    tmp_path: Path,
    *,
    writes_enabled: bool = True,
) -> tuple[IBKRPaperBroker, FakePaperTransport]:
    transport = FakePaperTransport()
    broker = IBKRPaperBroker(
        path=tmp_path / "ibkr-paper.sqlite3",
        transport=transport,
        paper_account="DU7654321",
        writes_enabled=writes_enabled,
    )
    broker.connect(host="127.0.0.1", client_id=91)
    return broker, transport


def test_paper_broker_uses_4002_and_maps_paper_account_state(tmp_path: Path) -> None:
    broker, transport = _broker(tmp_path)

    account = broker.get_account()
    positions = broker.list_positions()

    assert transport.connected_to == ("127.0.0.1", PAPER_PORT, 91)
    assert account.status == "ACTIVE"
    assert account.equity == "100000"
    assert account.last_equity == "101000"
    assert account.buying_power == "400000"
    assert positions[0].symbol == "AAPL"
    assert positions[0].side == "long"
    assert positions[0].qty == "3"


def test_paper_broker_submits_a_protected_entry_once_after_clean_what_if(tmp_path: Path) -> None:
    broker, transport = _broker(tmp_path)
    request = PaperOrderRequest(
        client_order_id="tsv2-20260803-AAPL-entry",
        symbol="AAPL",
        qty=2,
        order_type="market",
        take_profit_price="210.00",
        stop_loss_price="190.00",
    )

    first = broker.submit_order_idempotent(request)
    replay = broker.submit_order_idempotent(request)

    assert first.id == "100"
    assert replay == first
    assert len(transport.submissions) == 1
    parent, target, stop = transport.submissions[0]
    assert parent.order_type == "MKT"
    assert parent.side == "BUY"
    assert parent.transmit is False
    assert target.order_type == "LMT"
    assert target.parent_index == 0
    assert target.transmit is False
    assert stop.order_type == "STP"
    assert stop.parent_index == 0
    assert stop.transmit is True


def test_paper_broker_rejects_warning_and_disabled_writes_before_submission(tmp_path: Path) -> None:
    broker, transport = _broker(tmp_path)
    transport.what_if_warning = "margin warning"
    request = PaperOrderRequest(
        client_order_id="tsv2-20260803-AAPL-warning",
        symbol="AAPL",
        qty=1,
        order_type="market",
        stop_loss_price="190.00",
    )

    with pytest.raises(RuntimeError, match="what_if_warning"):
        broker.submit_order_idempotent(request)

    disabled, disabled_transport = _broker(tmp_path, writes_enabled=False)
    with pytest.raises(BrokerWritesDisabledError):
        disabled.submit_close_order_idempotent(
            PaperCloseRequest(
                client_order_id="tsv2-20260803-AAPL-close",
                symbol="AAPL",
                qty=1,
            )
        )
    assert not disabled_transport.submissions
