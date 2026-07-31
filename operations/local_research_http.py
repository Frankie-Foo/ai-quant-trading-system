"""Ephemeral-auth loopback HTTP interface for the macOS local research runtime."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from operations.local_research_runtime import LocalResearchRuntime


def build_local_research_http_server(
    runtime: LocalResearchRuntime,
    *,
    host: str,
    port: int,
    bearer_token: str,
) -> ThreadingHTTPServer:
    """Build a loopback-only, non-executable runtime interface."""

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("macOS local research runtime must bind loopback")
    if len(bearer_token) < 24 or "\r" in bearer_token or "\n" in bearer_token:
        raise ValueError("local runtime bearer token must be at least 24 safe characters")

    class Handler(BaseHTTPRequestHandler):
        server_version = "MacOSLocalResearchRuntime/1"

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                self._send(
                    HTTPStatus.UNAUTHORIZED,
                    {
                        "error": "local runtime authentication required",
                        "orders_authorized": False,
                    },
                )
                return
            path = urlparse(self.path).path
            observed_at = datetime.now(UTC)
            if path == "/v1/health":
                self._send(HTTPStatus.OK, runtime.status(observed_at))
                return
            if path == "/v1/desk":
                self._send(HTTPStatus.OK, runtime.snapshot(observed_at))
                return
            if path == "/v1/workflows":
                self._send(HTTPStatus.OK, runtime.workflow_status())
                return
            self._send(
                HTTPStatus.NOT_FOUND,
                {"error": "route not found", "orders_authorized": False},
            )

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._send(
                    HTTPStatus.UNAUTHORIZED,
                    {
                        "error": "local runtime authentication required",
                        "orders_authorized": False,
                    },
                )
                return
            path = urlparse(self.path).path
            if path == "/v1/run-due":
                self._send(HTTPStatus.OK, runtime.run_due(datetime.now(UTC)))
                return
            if path.startswith("/v1/workflows/"):
                action = path.rsplit("/", 1)[-1]
                query = parse_qs(urlparse(self.path).query)
                raw_date = query.get("trade_date", [None])[0]
                try:
                    trade_date = date.fromisoformat(raw_date) if raw_date else date.today()
                    result = runtime.submit_workflow(action, trade_date)
                except (TypeError, ValueError) as exc:
                    self._send(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "error": str(exc),
                            "orders_authorized": False,
                        },
                    )
                    return
                self._send(HTTPStatus.ACCEPTED, result)
                return
            if path in {"/v1/monitor/start", "/v1/monitor/stop"}:
                if path.endswith("/stop"):
                    self._send(HTTPStatus.OK, runtime.stop_monitor())
                    return
                query = parse_qs(urlparse(self.path).query)
                raw_date = query.get("trade_date", [None])[0]
                try:
                    trade_date = date.fromisoformat(raw_date) if raw_date else date.today()
                except (TypeError, ValueError) as exc:
                    self._send(
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc), "orders_authorized": False},
                    )
                    return
                self._send(HTTPStatus.ACCEPTED, runtime.start_monitor(trade_date))
                return
            self._send(
                HTTPStatus.NOT_FOUND,
                {"error": "route not found", "orders_authorized": False},
            )

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            prefix = "Bearer "
            return header.startswith(prefix) and secrets.compare_digest(
                header[len(prefix) :],
                bearer_token,
            )

        def _send(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)
