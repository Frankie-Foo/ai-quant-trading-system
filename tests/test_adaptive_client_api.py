from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

from kernel.adaptive_trade_plan import BaselineTradePlan, PlanMode
from operations.adaptive_client_api import (
    AdaptiveClientApplication,
    encode_plan_event,
)
from operations.adaptive_plan_store import AdaptivePlanStore
from operations.emergency_stop import EmergencyStopStore

OPEN = datetime(2026, 7, 28, 13, 30, tzinfo=UTC)


def _store(tmp_path: Path) -> AdaptivePlanStore:
    store = AdaptivePlanStore(tmp_path / "adaptive.sqlite3")
    store.register(
        BaselineTradePlan(
            plan_id="plan-20260728-XYZ",
            symbol="XYZ",
            trade_date=date(2026, 7, 28),
            mode=PlanMode.CATALYST,
            entry_window_end_utc=OPEN + timedelta(hours=2),
            force_exit_utc=OPEN + timedelta(hours=6, minutes=25),
            hard_stop=99.0,
            max_risk_dollars=300.0,
            max_notional=20_000.0,
            probe_fraction=0.25,
            max_spread_ratio=0.0025,
            soft_cooldown=timedelta(minutes=3),
            max_soft_revisions=3,
        )
    )
    return store


def test_health_and_dashboard_are_read_only_and_explicitly_non_executable(
    tmp_path: Path,
) -> None:
    app = AdaptiveClientApplication(store=_store(tmp_path))

    health = app.handle("GET", "/v1/health", {})
    dashboard = app.handle("GET", "/v1/dashboard", {})
    rejected = app.handle("POST", "/v1/orders", {})

    assert health.status == 200
    assert health.body["status"] == "ready"
    assert health.body["orders_authorized"] is False
    assert dashboard.status == 200
    plans = cast(list[dict[str, object]], dashboard.body["plans"])
    assert plans[0]["symbol"] == "XYZ"
    assert rejected.status == 405


def test_event_page_validates_cursor_and_has_no_secret_fields(tmp_path: Path) -> None:
    app = AdaptiveClientApplication(store=_store(tmp_path))

    response = app.handle("GET", "/v1/events", {"after": ["0"], "limit": ["10"]})
    invalid = app.handle("GET", "/v1/events", {"after": ["bad"]})

    assert response.status == 200
    assert response.body["events"] == []
    assert response.body["orders_authorized"] is False
    assert invalid.status == 400
    serialized = json.dumps(response.body).lower()
    assert "api_secret" not in serialized
    assert "app_secret" not in serialized


def test_sse_encoder_uses_sequence_as_cursor_and_utf8_json() -> None:
    encoded = encode_plan_event(
        {
            "sequence": 7,
            "plan_id": "plan-1",
            "observed_at_utc": OPEN.isoformat(),
            "event": {"action": "enter_probe", "reasons": ["两根分钟线确认"]},
        }
    )

    assert encoded.startswith(b"id: 7\nevent: plan-decision\n")
    assert "两根分钟线确认" in encoded.decode("utf-8")
    assert encoded.endswith(b"\n\n")


def test_only_global_emergency_stop_is_mutating_and_it_persists(
    tmp_path: Path,
) -> None:
    stop_path = tmp_path / "emergency.sqlite3"
    app = AdaptiveClientApplication(
        store=_store(tmp_path),
        emergency_stop=EmergencyStopStore(stop_path),
    )

    activated = app.handle("POST", "/v1/emergency-stop", {})
    replay = AdaptiveClientApplication(
        store=_store(tmp_path),
        emergency_stop=EmergencyStopStore(stop_path),
    ).handle("GET", "/v1/health", {})
    order_attempt = app.handle("POST", "/v1/orders", {})

    assert activated.status == 200
    assert activated.body["emergency_stop_active"] is True
    assert replay.body["emergency_stop_active"] is True
    assert order_attempt.status == 405
