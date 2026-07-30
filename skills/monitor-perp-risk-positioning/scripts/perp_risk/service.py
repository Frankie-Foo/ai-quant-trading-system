"""End-to-end collection, scoring, persistence, and notification service."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from . import __version__
from .config import AppConfig
from .engine import SentimentEngine
from .liquidations import (
    LiquidationSource,
    NullLiquidationSource,
    apply_liquidations,
    build_liquidation_source,
)
from .models import PerpObservation, ProviderStatus, RiskSnapshot, require_utc
from .notifications import WebhookNotifier
from .positioning import PositionResolver, PositionState
from .providers import (
    AevoClient,
    HyperliquidClient,
    ProviderError,
    ProviderFetch,
)
from .session import session_state
from .store import RiskStore


class PerpProvider(Protocol):
    def fetch(self, bindings: tuple[object, ...]) -> ProviderFetch: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class RunResult:
    snapshot: RiskSnapshot
    notification_status: str


class RiskService:
    def __init__(
        self,
        *,
        config: AppConfig,
        store: RiskStore,
        hyperliquid: HyperliquidClient | None = None,
        aevo: AevoClient | None = None,
        liquidation_source: LiquidationSource | None = None,
        notifier: WebhookNotifier | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self.store = store
        self._hyperliquid = hyperliquid or HyperliquidClient(
            collection=config.collection,
            http=config.http,
        )
        self._aevo = aevo or AevoClient(
            collection=config.collection,
            http=config.http,
        )
        self._liquidation = liquidation_source or build_liquidation_source(
            config.liquidation,
            config.http,
        )
        self._notifier = notifier or WebhookNotifier(config.notification)
        self._clock = clock or (lambda: datetime.now(UTC))

    def close(self) -> None:
        self._hyperliquid.close()
        self._aevo.close()
        close_liquidation = getattr(self._liquidation, "close", None)
        if callable(close_liquidation):
            close_liquidation()
        self._notifier.close()

    def run_snapshot(
        self,
        *,
        persist: bool = True,
        notify: bool = True,
    ) -> RunResult:
        started_at = require_utc(self._clock(), name="service clock")
        observations: list[PerpObservation] = []
        statuses: list[ProviderStatus] = []
        warnings: list[str] = []
        for venue, provider in (
            ("hyperliquid", self._hyperliquid),
            ("aevo", self._aevo),
        ):
            try:
                fetched = provider.fetch(self.config.bindings)
                observations.extend(fetched.observations)
                statuses.append(fetched.status)
            except ProviderError:
                statuses.append(
                    ProviderStatus(
                        venue=venue,
                        status="unavailable",
                        observation_count=0,
                        warnings=("provider_request_failed",),
                    )
                )
                warnings.append(f"{venue}_unavailable")
        current = _unique_observations(tuple(observations))
        liquidation_complete = not isinstance(
            self._liquidation,
            NullLiquidationSource,
        )
        if liquidation_complete:
            try:
                events = self._liquidation.fetch(
                    start_utc=started_at
                    - timedelta(seconds=self.config.collection.flow_window_seconds),
                    end_utc=require_utc(
                        self._clock(),
                        name="service clock",
                    ),
                )
                current = apply_liquidations(
                    current,
                    events,
                    source_complete=True,
                )
            except ProviderError:
                warnings.append("liquidation_provider_unavailable")
        else:
            warnings.append("liquidation_provider_not_configured")
        asof = require_utc(self._clock(), name="service clock")
        data_cutoff = max(
            (item.observed_at_utc for item in current),
            default=asof,
        )
        if data_cutoff > asof:
            asof = data_cutoff
        previous = self.store.previous_for_current(
            current,
            max_gap_seconds=self.config.collection.max_previous_gap_seconds,
        )
        signals = SentimentEngine(self.config).evaluate(
            observations=current,
            previous_observations=previous,
            asof_utc=asof,
        )
        prior_states = self.store.position_states()
        resolver = PositionResolver(
            self.config.policy,
            window_seconds=self.config.collection.flow_window_seconds,
        )
        targets = []
        states: list[PositionState] = []
        for signal in signals:
            assessment, state = resolver.resolve(
                signal,
                prior_states.get(signal.target_id),
            )
            targets.append(assessment)
            states.append(state)
        actionable, state_name = session_state(asof, self.config.session)
        snapshot = RiskSnapshot(
            skill_version=__version__,
            snapshot_id=(f"perp_{asof.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:12]}"),
            asof_utc=asof,
            data_cutoff_utc=data_cutoff,
            config_hash=self.config.config_hash,
            actionable=actionable,
            session_state=state_name,
            provider_status=tuple(statuses),
            targets=tuple(targets),
            warnings=tuple(dict.fromkeys(warnings)),
            production_eligible=False,
            execution_eligible=False,
            orders_submitted=0,
        )
        notification_status = "not_requested"
        if persist:
            self.store.persist_snapshot(
                snapshot,
                observations=current,
                states=tuple(states),
            )
            self.store.purge_observation_details(
                retention_days=self.config.storage.detail_retention_days
            )
            _atomic_write_json(
                self.config.latest_json_path,
                snapshot.model_dump_json(indent=2),
            )
            if notify:
                try:
                    notification_status = self._notifier.maybe_send(
                        snapshot,
                        store=self.store,
                    )
                except RuntimeError:
                    notification_status = "failed"
        return RunResult(
            snapshot=snapshot,
            notification_status=notification_status,
        )


def _unique_observations(
    observations: tuple[PerpObservation, ...],
) -> tuple[PerpObservation, ...]:
    keys = [item.key for item in observations]
    if len(keys) != len(set(keys)):
        raise ValueError("providers returned duplicate observations")
    return tuple(sorted(observations, key=lambda item: item.key))


def _atomic_write_json(path: Path, content: str) -> None:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(content + os.linesep, encoding="utf-8")
    temporary.replace(target)
