"""Local-only macOS research runtime with a fail-closed market-data seam."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import SecretStr

from data_plane.providers.alpaca_proxy import probe_alpaca_proxy_sip
from operations.client_desk import TradingDeskEvidence

MARKET_DATA_REQUIREMENTS = (
    "CLOUD_MARKET_DATA_API_TOKEN",
    "CLOUD_PLATFORM_BASE_URL",
    "MASSIVE_API_KEY",
    "SEC_USER_AGENT",
)
ALPACA_PROXY_REQUIREMENTS = ("ALPACA_PROXY_KEY", "ALPACA_PROXY_SECRET")
ProxyProbe = Callable[..., dict[str, object]]


class MarketDataAdapter(Protocol):
    """Prepare the immutable inputs consumed by the local research pipeline."""

    def status(self) -> dict[str, object]: ...


class ResearchPipeline(Protocol):
    """Run every due local research stage without exposing its internal DAG."""

    def run_due(
        self,
        *,
        now_utc: datetime,
        data_root: Path,
        runs_root: Path,
    ) -> dict[str, object]: ...


class UnconfiguredMarketDataAdapter:
    """Default adapter: no source, no download, and no invented fallback."""

    def status(self) -> dict[str, object]:
        return {
            "schema_version": "macos_market_data_adapter.v1",
            "provider_id": "unconfigured",
            "configured": False,
            "healthy": False,
            "reason": "market_data_provider_unconfigured",
            "missing_requirements": [],
            "can_run_pipeline": False,
        }


class EnvironmentMarketDataAdapter:
    """Compatibility adapter for the existing Massive/cloud-input pipeline."""

    def __init__(self, *, environ: Mapping[str, str] | None = None):
        self._environ = os.environ if environ is None else environ

    def status(self) -> dict[str, object]:
        missing = sorted(
            name
            for name in MARKET_DATA_REQUIREMENTS
            if not str(self._environ.get(name, "")).strip()
        )
        configured = not missing
        return {
            "schema_version": "macos_market_data_adapter.v1",
            "provider_id": "environment",
            "configured": configured,
            "healthy": configured,
            "reason": None if configured else "market_data_requirements_missing",
            "missing_requirements": missing,
            "can_run_pipeline": configured,
        }


def _run_alpaca_proxy_probe(
    *, key_id: SecretStr, secret_key: SecretStr
) -> dict[str, object]:
    return asyncio.run(
        probe_alpaca_proxy_sip(key_id=key_id, secret_key=secret_key)
    )


class AlpacaProxyMarketDataAdapter:
    """Fixed Alpaca proxy for realtime SIP; research history remains separate."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        probe: ProxyProbe = _run_alpaca_proxy_probe,
    ):
        self._environ = os.environ if environ is None else environ
        self._probe = probe
        self._cached_status: dict[str, object] | None = None

    def status(self) -> dict[str, object]:
        missing = sorted(
            name
            for name in ALPACA_PROXY_REQUIREMENTS
            if not str(self._environ.get(name, "")).strip()
        )
        if missing:
            return {
                "schema_version": "macos_market_data_adapter.v1",
                "provider_id": "alpaca_proxy_sip",
                "configured": False,
                "healthy": False,
                "reason": "credentials_missing",
                "missing_requirements": missing,
                "can_run_pipeline": False,
                "realtime_ready": False,
                "research_inputs_ready": False,
                "endpoint_host": "alpaca-trade-api.vertu.cn",
                "capabilities": ["bars", "quotes", "trades"],
            }
        if self._cached_status is None:
            try:
                probe_status = self._probe(
                    key_id=SecretStr(
                        str(self._environ["ALPACA_PROXY_KEY"]).strip()
                    ),
                    secret_key=SecretStr(
                        str(self._environ["ALPACA_PROXY_SECRET"]).strip()
                    ),
                )
            except Exception:
                probe_status = {
                    "healthy": False,
                    "reason": "connection_failed",
                    "endpoint_host": "alpaca-trade-api.vertu.cn",
                    "capabilities": ["bars", "quotes", "trades"],
                }
            healthy = probe_status.get("healthy") is True
            raw_capabilities = probe_status.get("capabilities")
            capabilities = (
                [str(value) for value in raw_capabilities]
                if isinstance(raw_capabilities, list)
                else ["bars", "quotes", "trades"]
            )
            self._cached_status = {
                "schema_version": "macos_market_data_adapter.v1",
                "provider_id": "alpaca_proxy_sip",
                "configured": True,
                "healthy": healthy,
                "reason": (
                    "historical_research_inputs_missing"
                    if healthy
                    else str(probe_status.get("reason") or "connection_failed")
                ),
                "missing_requirements": [],
                "can_run_pipeline": False,
                "realtime_ready": healthy,
                "research_inputs_ready": False,
                "endpoint_host": str(
                    probe_status.get("endpoint_host")
                    or "alpaca-trade-api.vertu.cn"
                ),
                "capabilities": capabilities,
            }
        cached_capabilities = self._cached_status.get("capabilities")
        return {
            **self._cached_status,
            "capabilities": (
                [str(value) for value in cached_capabilities]
                if isinstance(cached_capabilities, list)
                else []
            ),
        }


class ScheduledResearchPipeline:
    """Deep local module over the existing deterministic pre/postmarket DAGs."""

    def run_due(
        self,
        *,
        now_utc: datetime,
        data_root: Path,
        runs_root: Path,
    ) -> dict[str, object]:
        from schedule.postmarket import run as run_postmarket
        from schedule.premarket import run as run_premarket

        _require_utc(now_utc)
        data_root.mkdir(parents=True, exist_ok=True)
        runs_root.mkdir(parents=True, exist_ok=True)
        shared = [
            "--data-root",
            str(data_root),
            "--state-db",
            str(runs_root / "jobs.sqlite3"),
        ]
        premarket_code = run_premarket(
            [
                *shared,
                "--lock-file",
                str(runs_root / "premarket.lock"),
            ],
            now_utc=now_utc,
        )
        postmarket_code = run_postmarket(
            [
                *shared,
                "--lock-file",
                str(runs_root / "postmarket.lock"),
                "--llm-mode",
                "off",
            ]
        )
        status = "complete" if premarket_code == 0 and postmarket_code == 0 else "failed"
        return {
            "schema_version": "macos_local_research_tick.v1",
            "status": status,
            "observed_at_utc": now_utc.isoformat(),
            "premarket_exit_code": premarket_code,
            "postmarket_exit_code": postmarket_code,
            "orders_submitted": 0,
        }


class LocalResearchRuntime:
    """One small interface over local selection, review, Agent evidence, and scheduling."""

    def __init__(
        self,
        *,
        data_root: Path,
        runs_root: Path,
        market_data: MarketDataAdapter,
        pipeline: ResearchPipeline | None = None,
    ):
        self.data_root = data_root
        self.runs_root = runs_root
        self.market_data = market_data
        self.pipeline = pipeline or ScheduledResearchPipeline()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self._desk = TradingDeskEvidence(
            data_root=self.data_root,
            runs_root=self.runs_root,
        )

    def status(self, observed_at_utc: datetime | None = None) -> dict[str, object]:
        observed_at = observed_at_utc or datetime.now(UTC)
        _require_utc(observed_at)
        return {
            "schema_version": "macos_local_research_runtime.v1",
            "observed_at_utc": observed_at.isoformat(),
            "execution_mode": "local_research",
            "local_execution": True,
            "research_kernel": "trading-system-v2",
            "market_data": self.market_data.status(),
            "orders_authorized": False,
            "paper_runtime_authorized": False,
            "live_trading_authorized": False,
        }

    def snapshot(self, observed_at_utc: datetime | None = None) -> dict[str, object]:
        observed_at = observed_at_utc or datetime.now(UTC)
        _require_utc(observed_at)
        status = self.status(observed_at)
        desk = self._desk.snapshot(observed_at)
        market_data = status["market_data"]
        if not isinstance(market_data, dict):
            raise TypeError("market-data adapter status must be an object")
        if market_data.get("can_run_pipeline") is not True:
            selection = desk.get("selection")
            if isinstance(selection, dict) and selection.get("status") != "ready":
                selection["status"] = "blocked"
                selection["blocker"] = str(
                    market_data.get("reason")
                    or "market_data_provider_unconfigured"
                )
            desk["pipeline_status"] = "blocked"
        desk["runtime"] = status
        desk["orders_authorized"] = False
        desk["paper_eligible"] = False
        desk["live_eligible"] = False
        return desk

    def run_due(self, observed_at_utc: datetime | None = None) -> dict[str, object]:
        observed_at = observed_at_utc or datetime.now(UTC)
        _require_utc(observed_at)
        market_data = self.market_data.status()
        if market_data.get("can_run_pipeline") is not True:
            return {
                "schema_version": "macos_local_research_tick.v1",
                "status": "blocked",
                "observed_at_utc": observed_at.isoformat(),
                "reason": str(
                    market_data.get("reason")
                    or "market_data_provider_unconfigured"
                ),
                "orders_submitted": 0,
            }
        result = self.pipeline.run_due(
            now_utc=observed_at,
            data_root=self.data_root,
            runs_root=self.runs_root,
        )
        return {**result, "orders_submitted": 0}


def _require_utc(value: datetime) -> None:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None:
        raise ValueError("local research runtime timestamps must be timezone-aware")
    if offset.total_seconds() != 0:
        raise ValueError("local research runtime timestamps must use UTC")
