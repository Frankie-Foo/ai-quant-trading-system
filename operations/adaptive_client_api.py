"""Read-only local HTTP interface for the adaptive trading client."""

from __future__ import annotations

import json
import mimetypes
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from operations.adaptive_plan_store import AdaptivePlanStore
from operations.emergency_stop import EmergencyStopStore


def _event_sequence(event: dict[str, object]) -> int:
    value = event.get("sequence")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("adaptive event sequence is invalid")
    return value


@dataclass(frozen=True)
class ClientApiResponse:
    status: int
    body: dict[str, object]


def encode_plan_event(event: dict[str, object]) -> bytes:
    sequence = event.get("sequence")
    if not isinstance(sequence, int) or sequence <= 0:
        raise ValueError("plan event sequence must be a positive integer")
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"id: {sequence}\nevent: plan-decision\ndata: {payload}\n\n".encode()


class AdaptiveClientApplication:
    """Read-only plan interface plus a one-way global emergency stop."""

    def __init__(
        self,
        *,
        store: AdaptivePlanStore,
        emergency_stop: EmergencyStopStore | None = None,
    ):
        self.store = store
        self.emergency_stop = emergency_stop

    def handle(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
    ) -> ClientApiResponse:
        if method == "POST" and path == "/v1/emergency-stop":
            if self.emergency_stop is None:
                return ClientApiResponse(
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                    body={
                        "error": "emergency-stop store unavailable",
                        "orders_authorized": False,
                    },
                )
            state = self.emergency_stop.activate(
                at_utc=datetime.now(UTC),
                reason="desktop_global_stop",
            )
            return ClientApiResponse(
                status=HTTPStatus.OK,
                body={
                    "schema_version": "emergency_stop.v1",
                    "emergency_stop_active": state.active,
                    "activated_at_utc": (
                        state.activated_at_utc.isoformat()
                        if state.activated_at_utc is not None
                        else None
                    ),
                    "orders_authorized": False,
                },
            )
        if method != "GET":
            return ClientApiResponse(
                status=HTTPStatus.METHOD_NOT_ALLOWED,
                body={
                    "error": "client interface is read-only",
                    "orders_authorized": False,
                },
            )
        if path == "/v1/health":
            stop_active = (
                self.emergency_stop.read().active
                if self.emergency_stop is not None
                else False
            )
            return ClientApiResponse(
                status=HTTPStatus.OK,
                body={
                    "schema_version": "adaptive_client_health.v1",
                    "status": "ready",
                    "orders_authorized": False,
                    "emergency_stop_active": stop_active,
                    "execution_mode": "alpaca_paper",
                    "live_trading_authorized": False,
                    "ui_order_entry_enabled": False,
                },
            )
        if path == "/v1/dashboard":
            return ClientApiResponse(
                status=HTTPStatus.OK,
                body=self.store.dashboard(),
            )
        if path == "/v1/events":
            try:
                after = self._integer_query(query, "after", default=0)
                limit = self._integer_query(query, "limit", default=100)
                events = self.store.events_after(after, limit=limit)
            except (TypeError, ValueError):
                return ClientApiResponse(
                    status=HTTPStatus.BAD_REQUEST,
                    body={
                        "error": "after and limit must be valid integers",
                        "orders_authorized": False,
                    },
                )
            return ClientApiResponse(
                status=HTTPStatus.OK,
                body={
                    "schema_version": "adaptive_plan_events.v1",
                    "events": list(events),
                    "next_sequence": (
                        after if not events else _event_sequence(events[-1])
                    ),
                    "orders_authorized": False,
                },
            )
        return ClientApiResponse(
            status=HTTPStatus.NOT_FOUND,
            body={"error": "route not found", "orders_authorized": False},
        )

    @staticmethod
    def _integer_query(
        query: dict[str, list[str]],
        name: str,
        *,
        default: int,
    ) -> int:
        values = query.get(name)
        if not values:
            return default
        if len(values) != 1:
            raise ValueError("query value must be singular")
        return int(values[0])


def _safe_static_path(root: Path, request_path: str) -> Path | None:
    relative = request_path.lstrip("/") or "index.html"
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        return None
    if candidate.is_dir():
        candidate = candidate / "index.html"
    if candidate.is_file():
        return candidate
    fallback = resolved_root / "index.html"
    return fallback if fallback.is_file() and "." not in Path(relative).name else None


def build_client_http_server(
    application: AdaptiveClientApplication,
    *,
    host: str,
    port: int,
    static_root: Path,
) -> ThreadingHTTPServer:
    """Build a localhost-oriented server with dashboard JSON and SSE decisions."""

    if not static_root.is_dir():
        raise FileNotFoundError(f"client static root does not exist: {static_root}")

    class Handler(BaseHTTPRequestHandler):
        server_version = "AdaptiveTradingClient/1"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/v1/events/stream":
                self._serve_event_stream(parse_qs(parsed.query))
                return
            if parsed.path.startswith("/v1/"):
                response = application.handle(
                    "GET",
                    parsed.path,
                    parse_qs(parsed.query),
                )
                self._send_json(response)
                return
            target = _safe_static_path(static_root, parsed.path)
            if target is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            content = target.read_bytes()
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", f"{mime}; charset=utf-8")
            self.send_header(
                "Cache-Control",
                "no-cache" if target.name == "index.html" else "public, max-age=3600",
            )
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            response = application.handle(
                "POST",
                parsed.path,
                parse_qs(parsed.query),
            )
            self._send_json(response)

        def _send_json(self, response: ClientApiResponse) -> None:
            body = json.dumps(
                response.body,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(response.status)
            self._security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_event_stream(self, query: dict[str, list[str]]) -> None:
            header_cursor = self.headers.get("Last-Event-ID", "").strip()
            raw_cursor = query.get("after", [header_cursor or "0"])
            try:
                after = int(raw_cursor[0])
                if after < 0:
                    raise ValueError
            except (ValueError, IndexError):
                self._send_json(
                    ClientApiResponse(
                        status=HTTPStatus.BAD_REQUEST,
                        body={
                            "error": "invalid event cursor",
                            "orders_authorized": False,
                        },
                    )
                )
                return
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            deadline = time.monotonic() + 25.0
            cursor = after
            try:
                while time.monotonic() < deadline:
                    events = application.store.events_after(cursor)
                    if events:
                        for event in events:
                            self.wfile.write(encode_plan_event(event))
                            cursor = _event_sequence(event)
                        self.wfile.flush()
                    else:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                    time.sleep(1.0)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; connect-src 'self'; "
                "img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                "script-src 'self'",
            )

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)
