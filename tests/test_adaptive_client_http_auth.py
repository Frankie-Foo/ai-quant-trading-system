from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from operations.adaptive_client_api import (
    AdaptiveClientApplication,
    build_client_http_server,
)
from operations.adaptive_plan_store import AdaptivePlanStore


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
