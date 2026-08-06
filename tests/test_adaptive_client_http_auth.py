from __future__ import annotations

import json
import threading
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from operations.adaptive_client_api import (
    AdaptiveClientApplication,
    build_client_http_server,
)
from operations.adaptive_plan_store import AdaptivePlanStore
from operations.emergency_stop import EmergencyStopStore


def _request(
    url: str,
    *,
    token: str | None = None,
) -> tuple[int, dict[str, object]]:
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=5) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def test_optional_bearer_token_protects_remote_read_endpoints(
    tmp_path: Path,
) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("<html></html>", encoding="utf-8")
    application = AdaptiveClientApplication(
        store=AdaptivePlanStore(tmp_path / "plans.sqlite3"),
    )
    server = build_client_http_server(
        application,
        host="127.0.0.1",
        port=0,
        static_root=static_root,
        bearer_token="one-user-read-token",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        missing_status, missing = _request(f"{base_url}/v1/health")
        wrong_status, wrong = _request(
            f"{base_url}/v1/health",
            token="wrong-token",
        )
        ok_status, ok = _request(
            f"{base_url}/v1/health",
            token="one-user-read-token",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert missing_status == 401
    assert wrong_status == 401
    assert ok_status == 200
    assert missing["orders_authorized"] is False
    assert wrong["orders_authorized"] is False
    assert ok["orders_authorized"] is False
    serialized = json.dumps([missing, wrong, ok])
    assert "one-user-read-token" not in serialized



def test_cross_site_request_cannot_trigger_global_emergency_stop(
    tmp_path: Path,
) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("<html></html>", encoding="utf-8")
    emergency_stop = EmergencyStopStore(tmp_path / "emergency.sqlite3")
    server = build_client_http_server(
        AdaptiveClientApplication(
            store=AdaptivePlanStore(tmp_path / "plans.sqlite3"),
            emergency_stop=emergency_stop,
        ),
        host="127.0.0.1",
        port=0,
        static_root=static_root,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        attack = Request(
            f"{base_url}/v1/emergency-stop",
            data=b"",
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://evil.example",
            },
        )
        try:
            urlopen(attack, timeout=5)  # noqa: S310
        except HTTPError as error:
            attack_status = error.code
            attack_body = json.loads(error.read())
        allowed = Request(
            f"{base_url}/v1/emergency-stop",
            data=b"",
            method="POST",
            headers={"X-Adaptive-Client-Action": "emergency-stop-v1"},
        )
        with urlopen(allowed, timeout=5) as response:  # noqa: S310
            allowed_status = response.status
            allowed_body = json.loads(response.read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert attack_status == 403
    assert attack_body["emergency_stop_active"] is False
    assert allowed_status == 200
    assert allowed_body["emergency_stop_active"] is True


def test_browser_session_cookie_keeps_bearer_protected_ui_usable(
    tmp_path: Path,
) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("<html></html>", encoding="utf-8")
    server = build_client_http_server(
        AdaptiveClientApplication(
            store=AdaptivePlanStore(tmp_path / "plans.sqlite3"),
        ),
        host="127.0.0.1",
        port=0,
        static_root=static_root,
        bearer_token="one-user-read-token",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    cookie_jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    try:
        with opener.open(f"{base_url}/", timeout=5) as response:  # noqa: S310
            assert response.status == 200
            set_cookie = response.headers.get("Set-Cookie", "")
        with opener.open(f"{base_url}/v1/health", timeout=5) as response:  # noqa: S310
            health_status = response.status
            health = json.loads(response.read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie
    assert health_status == 200
    assert health["orders_authorized"] is False
    assert "one-user-read-token" not in set_cookie


def test_http_server_rejects_non_loopback_binding(tmp_path: Path) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("<html></html>", encoding="utf-8")
    try:
        build_client_http_server(
            AdaptiveClientApplication(
                store=AdaptivePlanStore(tmp_path / "plans.sqlite3"),
            ),
            host="0.0.0.0",
            port=0,
            static_root=static_root,
        )
    except ValueError as error:
        assert "localhost-only" in str(error)
    else:
        raise AssertionError("non-loopback binding must be rejected")
