"""Paper-only autonomous execution control plane for the local client.

This module is deliberately separate from the manual 4001 execution desk.  A
frozen autonomous-Paper plan and its safety envelope are mandatory; no LLM text
or renderer input can be converted directly into a broker order.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from execution.autonomous_paper_session import (
    AutonomousPaperBroker,
    PaperSessionLedger,
    PaperSessionOrchestrator,
)
from execution.ibkr_paper_broker import IBKRPaperBroker
from execution.ibkr_tws_adapter import OfficialIbapiPaperAdapter
from execution.sip_store import SipEventStore
from operations.adaptive_plan_adapters import PlanEvidence, SipStoreMarketFactsAdapter
from operations.autonomous_paper_config import (
    AutonomousPaperRuntimeConfig,
    load_autonomous_paper_config,
)
from operations.autonomous_paper_runtime import AutonomousPaperRuntime
from operations.autonomous_policy_adapter import load_runtime_safety_envelope

NEW_YORK = ZoneInfo("America/New_York")
PAPER_CONFIG_NAME = "approved-autonomous-paper.json"


class PaperAutopilot:
    """A tiny start/stop boundary around the pre-existing deterministic runner."""

    def __init__(
        self,
        *,
        data_root: Path,
        runs_root: Path,
        environ: Mapping[str, str] | None = None,
        broker_factory: Callable[[bool, str], AutonomousPaperBroker] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.data_root = data_root
        self.runs_root = runs_root
        self.environ = os.environ if environ is None else environ
        self._broker_factory = broker_factory or self._default_broker
        self._now = now
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._broker: AutonomousPaperBroker | None = None
        self._connected = False
        self._running = False
        self._account_masked = ""
        self._last_error = ""
        self._last_tick_at_utc = ""
        self._last_outcomes: tuple[dict[str, object], ...] = ()
        self._plan_status = "missing"
        self._plan_error = ""

    @property
    def plan_path(self) -> Path:
        return self.runs_root / "paper-autopilot" / PAPER_CONFIG_NAME

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "schema_version": "ibkr.paper_autopilot.v1",
                "mode": "paper",
                "port": 4002,
                "configured": self._profile_error() is None,
                "connected": self._connected,
                "running": self._running,
                "paper_writes_armed": self._running,
                "account_masked": self._account_masked,
                "arm_confirmation_phrase": self._arm_phrase(),
                "plan_status": self._plan_status,
                "plan_error": self._plan_error,
                "last_tick_at_utc": self._last_tick_at_utc,
                "last_outcomes": [dict(item) for item in self._last_outcomes],
                "last_error": self._last_error,
                "root_research_orders_authorized": False,
            }

    def handle(self, command: dict[str, object]) -> dict[str, object]:
        kind = str(command.get("kind", ""))
        if kind == "connect":
            return self.connect()
        if kind == "disconnect":
            return self.stop_and_disconnect()
        if kind == "validate_plan":
            self._validate_plan()
            return self.snapshot()
        if kind == "start":
            confirmation = str(command.get("confirmation", ""))
            return self.start(confirmation=confirmation)
        if kind == "stop":
            return self.stop()
        raise ValueError("unknown_paper_autopilot_command")

    def connect(self) -> dict[str, object]:
        with self._lock:
            if self._running:
                return self.snapshot()
            host, client_id, account = self._profile()
            self._close_broker_locked()
            broker = self._broker_factory(False, account)
            connect = getattr(broker, "connect", None)
            if not callable(connect):
                raise RuntimeError("IBKR Paper broker connection is unavailable")
            try:
                connect(host=host, client_id=client_id)
                broker.get_account()
            except Exception as exc:
                close = getattr(broker, "close", None)
                if callable(close):
                    close()
                self._connected = False
                self._last_error = _safe_error_code(exc)
                raise RuntimeError(self._last_error) from exc
            self._broker = broker
            self._connected = True
            self._account_masked = _mask_account(account)
            self._last_error = ""
            return self.snapshot()

    def start(self, *, confirmation: str) -> dict[str, object]:
        with self._lock:
            if self._running:
                return self.snapshot()
            if confirmation != self._arm_phrase():
                raise ValueError("confirmation_mismatch")
            config = self._validate_plan()
            host, client_id, account = self._profile()
            self._close_broker_locked()
            broker = self._broker_factory(True, account)
            connect = getattr(broker, "connect", None)
            if not callable(connect):
                raise RuntimeError("IBKR Paper broker connection is unavailable")
            try:
                connect(host=host, client_id=client_id)
                broker.get_account()
            except Exception as exc:
                close = getattr(broker, "close", None)
                if callable(close):
                    close()
                self._last_error = _safe_error_code(exc)
                raise RuntimeError(self._last_error) from exc
            self._broker = broker
            self._connected = True
            self._account_masked = _mask_account(account)
            self._last_error = ""
            self._last_outcomes = ()
            self._stop = threading.Event()
            self._running = True
            self._thread = threading.Thread(
                target=self._run_loop,
                args=(config, broker),
                name="ibkr-paper-autopilot",
                daemon=True,
            )
            self._thread.start()
            return self.snapshot()

    def stop(self, *, timeout_seconds: float = 5.0) -> dict[str, object]:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout_seconds)
        with self._lock:
            if thread is not None and thread.is_alive():
                self._last_error = "stop_timeout"
                return self.snapshot()
            self._running = False
            self._thread = None
            self._close_broker_locked()
            return self.snapshot()

    def stop_and_disconnect(self) -> dict[str, object]:
        return self.stop()

    def _run_loop(
        self,
        config: AutonomousPaperRuntimeConfig,
        broker: AutonomousPaperBroker,
    ) -> None:
        evidence = {
            bundle.plan.plan_id: PlanEvidence(
                benchmark_symbol=bundle.benchmark_symbol,
                sector_symbol=bundle.sector_symbol,
                catalyst_score=(
                    None
                    if bundle.evidence.catalyst.value is None
                    else bundle.evidence.catalyst.value / 100.0
                ),
                provenance=(
                    f"{bundle.market_context_provenance}|"
                    f"{bundle.evidence.catalyst.provenance}"
                ),
            )
            for bundle in config.plans
        }
        runtime = AutonomousPaperRuntime(
            plans=config.plans,
            market=SipStoreMarketFactsAdapter(
                store=SipEventStore(self.runs_root / "sip-stream.sqlite3"),
                evidence=evidence,
            ),
            broker=broker,
            orchestrator=PaperSessionOrchestrator(
                broker=broker,
                ledger=PaperSessionLedger(
                    self.runs_root / "ibkr-paper-autopilot.sqlite3"
                ),
                paper_authorized=True,
                owned_symbols=frozenset(
                    bundle.plan.symbol for bundle in config.plans
                ),
            ),
            envelope_loader=load_runtime_safety_envelope,
        )
        try:
            while not self._stop.is_set():
                outcomes = runtime.tick_once(observed_at_utc=self._now())
                outcome_rows: list[dict[str, object]] = []
                for outcome in outcomes:
                    outcome_rows.append(
                        {
                        "plan_id": outcome.plan_id,
                        "symbol": outcome.symbol,
                        "action": outcome.result.action.value,
                        "reasons": list(outcome.result.reasons),
                        "degraded_reasons": list(outcome.degraded_reasons),
                        }
                    )
                compact = tuple(outcome_rows)
                with self._lock:
                    self._last_tick_at_utc = self._now().isoformat()
                    self._last_outcomes = compact
                    self._last_error = ""
                self._stop.wait(config.poll_seconds)
        except Exception as exc:
            with self._lock:
                self._last_error = _safe_error_code(exc)
        finally:
            close = getattr(broker, "close", None)
            if callable(close):
                close()
            with self._lock:
                self._connected = False
                self._running = False

    def _validate_plan(self) -> AutonomousPaperRuntimeConfig:
        try:
            config = load_autonomous_paper_config(self.plan_path)
            today = self._now().astimezone(NEW_YORK).date()
            for bundle in config.plans:
                if bundle.plan.trade_date != today:
                    raise ValueError("paper_plan_trade_date_mismatch")
                if not bundle.safety_envelope_path.is_file():
                    raise ValueError("paper_safety_envelope_missing")
                envelope = load_runtime_safety_envelope(bundle.safety_envelope_path)
                if not envelope.is_current(self._now()):
                    raise ValueError("paper_safety_envelope_stale")
        except Exception as exc:
            self._plan_status = "invalid" if self.plan_path.exists() else "missing"
            self._plan_error = _safe_error_code(exc)
            raise RuntimeError(self._plan_error) from exc
        self._plan_status = "valid"
        self._plan_error = ""
        return config

    def _profile(self) -> tuple[str, int, str]:
        error = self._profile_error()
        if error is not None:
            raise ValueError(error)
        host = str(self.environ.get("IBKR_PAPER_HOST", "")).strip()
        client_id = int(str(self.environ.get("IBKR_PAPER_CLIENT_ID", "")))
        account = str(self.environ.get("IBKR_PAPER_ACCOUNT", "")).strip().upper()
        return host, client_id, account

    def _profile_error(self) -> str | None:
        host = str(self.environ.get("IBKR_PAPER_HOST", "")).strip()
        client_id = str(self.environ.get("IBKR_PAPER_CLIENT_ID", "")).strip()
        account = str(self.environ.get("IBKR_PAPER_ACCOUNT", "")).strip().upper()
        if not host or len(host) > 253 or re.fullmatch(r"[A-Za-z0-9.-]+", host) is None:
            return "paper_profile_invalid"
        if re.fullmatch(r"\d+", client_id) is None or int(client_id) > 2_147_483_647:
            return "paper_profile_invalid"
        if re.fullmatch(r"DU[A-Z0-9-]{4,30}", account) is None:
            return "paper_profile_invalid"
        return None

    def _arm_phrase(self) -> str:
        account = str(self.environ.get("IBKR_PAPER_ACCOUNT", "")).strip().upper()
        return f"启用模拟盘自动执行 {_mask_account(account) if account else '当前账户'}"

    def _close_broker_locked(self) -> None:
        broker = self._broker
        self._broker = None
        self._connected = False
        if broker is None:
            return
        close = getattr(broker, "close", None)
        if callable(close):
            close()

    def _default_broker(self, writes_enabled: bool, account: str) -> AutonomousPaperBroker:
        return IBKRPaperBroker(
            path=self.runs_root / "ibkr-paper-orders.sqlite3",
            transport=OfficialIbapiPaperAdapter(
                api_read_only=False,
                expected_account_id=account,
            ),
            paper_account=account,
            writes_enabled=writes_enabled,
        )


def _mask_account(value: str) -> str:
    if not value:
        return ""
    return f"DU***{value[-4:]}"


def _safe_error_code(error: Exception) -> str:
    message = str(error).lower()
    if "timeout" in message:
        return "connection_timeout"
    if "connection" in message or "gateway" in message:
        return "connection_failed"
    if "account" in message:
        return "account_mismatch"
    if "safety" in message:
        return "paper_safety_envelope_invalid"
    if "plan" in message or "config" in message:
        return "paper_plan_invalid"
    if "confirmation" in message:
        return "confirmation_mismatch"
    return "paper_autopilot_failed"
