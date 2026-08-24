"""Alpaca Paper autopilot through the local cloud API on port 8765.

The deterministic plan, safety envelope, audit ledger, and stop boundary are
shared with the IBKR implementation. Only the provider profile and broker
connection seam differ; no IBKR 4002 connection is opened on this path.
"""

from __future__ import annotations

import re
from typing import cast
from urllib.parse import urlparse

from pydantic import SecretStr

from execution.alpaca_paper import CloudPaperBroker
from execution.autonomous_paper_session import AutonomousPaperBroker
from operations.ibkr_paper_autopilot import PaperAutopilot as _PaperAutopilot
from operations.paper_runtime_policy import reject_retired_paper_runtime

DEFAULT_BASE_URL = "http://127.0.0.1:8765"


class AlpacaPaperAutopilot(_PaperAutopilot):
    """Paper-only executor routed to the authenticated local 8765 API."""

    SCHEMA_VERSION = "alpaca.paper_autopilot.v1"
    PROVIDER_ID = "alpaca_paper_local_api"
    PAPER_PORT = 8765
    AUDIT_LEDGER_NAME = "alpaca-paper-autopilot.sqlite3"
    THREAD_NAME = "alpaca-paper-autopilot"
    SAFETY_PROVENANCE_PREFIX = "operations.alpaca_paper_autopilot"

    def start(self, *, confirmation: str) -> dict[str, object]:
        del confirmation
        reject_retired_paper_runtime("desktop-alpaca-autopilot")

    def _profile(self) -> tuple[str, int, str]:
        error = self._profile_error()
        if error is not None:
            raise ValueError(error)
        base_url = self._base_url()
        account = str(self.environ.get("ALPACA_PAPER_ACCOUNT", "PAPER")).strip()
        return base_url, 0, account or "PAPER"

    def _profile_error(self) -> str | None:
        base_url = self._base_url()
        parsed = urlparse(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port != 8765
        ):
            return "paper_profile_invalid"
        token = str(self.environ.get("CLOUD_PAPER_API_TOKEN", "")).strip()
        if len(token) < 24 or re.search(r"\s", token):
            return "paper_profile_invalid"
        account = str(self.environ.get("ALPACA_PAPER_ACCOUNT", "PAPER")).strip()
        if not account or len(account) > 64 or re.fullmatch(r"[A-Za-z0-9._-]+", account) is None:
            return "paper_profile_invalid"
        return None

    def _connect_broker(
        self,
        broker: AutonomousPaperBroker,
        *,
        host: str,
        client_id: int,
    ) -> None:
        del broker, host, client_id
        # CloudPaperBroker is HTTP-based and establishes connectivity on the
        # first authenticated account read; there is no TWS-style handshake.

    def _mask_account(self, value: str) -> str:
        normalized = value.strip() or "PAPER"
        if normalized == "PAPER":
            return "ALPACA-PAPER"
        return f"ALPACA***{normalized[-4:]}"

    def _arm_phrase(self) -> str:
        account = str(self.environ.get("ALPACA_PAPER_ACCOUNT", "PAPER")).strip()
        return f"启用Alpaca模拟盘自动执行 {self._mask_account(account)}"

    def _default_broker(
        self,
        writes_enabled: bool,
        account: str,
    ) -> AutonomousPaperBroker:
        del account
        token = str(self.environ.get("CLOUD_PAPER_API_TOKEN", "")).strip()
        return cast(
            AutonomousPaperBroker,
            CloudPaperBroker(
                base_url=self._base_url(),
                token=SecretStr(token),
                writes_enabled=writes_enabled,
            ),
        )

    def _base_url(self) -> str:
        return str(
            self.environ.get("CLOUD_PLATFORM_BASE_URL", DEFAULT_BASE_URL)
        ).strip().rstrip("/")
