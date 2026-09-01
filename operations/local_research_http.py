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


def _safe_execution_error_code(error: Exception) -> str:
    message = str(error).lower()
    rules = (
        (("multiple_accounts_require_selection",), "multiple_accounts_require_selection"),
        (("configured_account_not_visible",), "account_mismatch"),
        (("account_mismatch", "does not match"), "account_mismatch"),
        (("not bound",), "account_not_bound"),
        (("connection_timeout", "timed out", "timeout"), "connection_timeout"),
        (("gateway", "not connected", "connection"), "connection_failed"),
        (("read-only", "read only"), "api_read_only"),
        (("confirmation",), "confirmation_mismatch"),
        (("preview expired",), "preview_expired"),
        (("warning changed",), "preview_changed"),
        (("matching preview",), "preview_required"),
        (("recovery", "status unknown", "unknown order"), "recovery_required"),
        (("max_notional", "max order notional"), "max_notional_exceeded"),
        (("long position",), "reduce_long_exceeds_position"),
        (("duplicate", "active buy"), "duplicate_exposure"),
        (("rejected", "ibkr_error_"), "broker_rejected"),
    )
    for needles, code in rules:
        if any(needle in message for needle in needles):
            return code
    return "execution_failed"


def _safe_paper_error_code(error: Exception) -> str:
    message = str(error).lower()
    rules = (
        (("confirmation",), "confirmation_mismatch"),
        (("timeout",), "connection_timeout"),
        (("gateway", "connection"), "connection_failed"),
        (("account",), "account_mismatch"),
        (("safety",), "paper_safety_envelope_invalid"),
        (("plan", "config"), "paper_plan_invalid"),
        (("profile", "configured"), "paper_profile_invalid"),
    )
    for needles, code in rules:
        if any(needle in message for needle in needles):
            return code
    return "paper_autopilot_failed"


def build_local_research_http_server(
    runtime: LocalResearchRuntime,
    *,
    host: str,
    port: int,
    bearer_token: str,
) -> ThreadingHTTPServer:
    """Build the authenticated loopback research and manual-execution interface."""

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
            if path == "/v1/execution":
                self._send(HTTPStatus.OK, runtime.execution_snapshot())
                return
            if path == "/v1/paper-autopilot":
                self._send(HTTPStatus.OK, runtime.paper_autopilot_snapshot())
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
            if path == "/v1/execution/commands":
                try:
                    command = self._read_json_object()
                    result = runtime.handle_execution(command)
                except KeyError:
                    self._send(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "error": "invalid_execution_command",
                            "error_code": "invalid_execution_command",
                            "orders_authorized": False,
                        },
                    )
                    return
                except ValueError as exc:
                    error_code = _safe_execution_error_code(exc)
                    if error_code == "execution_failed":
                        error_code = "invalid_execution_command"
                    self._send(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "error": error_code,
                            "error_code": error_code,
                            "orders_authorized": False,
                        },
                    )
                    return
                except Exception as exc:
                    error_code = _safe_execution_error_code(exc)
                    self._send(
                        HTTPStatus.CONFLICT,
                        {
                            "error": error_code,
                            "error_code": error_code,
                            "orders_authorized": False,
                        },
                    )
                    return
                self._send(HTTPStatus.OK, result)
                return
            if path == "/v1/paper-autopilot/commands":
                try:
                    command = self._read_json_object()
                    result = runtime.handle_paper_autopilot(command)
                except (KeyError, ValueError) as exc:
                    error_code = _safe_paper_error_code(exc)
                    self._send(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "error": error_code,
                            "error_code": error_code,
                            "orders_authorized": False,
                        },
                    )
                    return
                except Exception as exc:
                    error_code = _safe_paper_error_code(exc)
                    self._send(
                        HTTPStatus.CONFLICT,
                        {
                            "error": error_code,
                            "error_code": error_code,
                            "orders_authorized": False,
                        },
                    )
                    return
                self._send(HTTPStatus.OK, result)
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

        def _read_json_object(self) -> dict[str, object]:
            raw_length = self.headers.get("Content-Length", "")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ValueError("request body length is invalid") from exc
            if length <= 0 or length > 65_536:
                raise ValueError("request body length is invalid")
            try:
                payload = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("request body must be valid JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError("execution command must be a JSON object")
            return {str(key): value for key, value in payload.items()}

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
