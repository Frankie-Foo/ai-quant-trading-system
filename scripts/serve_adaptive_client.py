"""Serve the read-only adaptive trading desktop client on localhost."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from operations.adaptive_client_api import (
    AdaptiveClientApplication,
    build_client_http_server,
)
from operations.adaptive_plan_store import AdaptivePlanStore
from operations.client_desk import TradingDeskEvidence
from operations.emergency_stop import EmergencyStopStore

ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--state-db",
        type=Path,
        default=ROOT / "runs" / "adaptive-plans.sqlite3",
    )
    parser.add_argument(
        "--static-root",
        type=Path,
        default=ROOT / "client" / "dist",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "the desktop client server is localhost-only; "
            "remote deployment requires the authenticated HTTPS gateway"
        )
    store = AdaptivePlanStore(args.state_db)
    emergency_stop = EmergencyStopStore(
        args.state_db.with_name("emergency-stop.sqlite3")
    )
    desk = TradingDeskEvidence(
        data_root=ROOT / "data",
        runs_root=ROOT / "runs",
    )
    application = AdaptiveClientApplication(
        store=store,
        emergency_stop=emergency_stop,
        desk_provider=lambda: desk.snapshot(datetime.now(UTC)),
    )
    server = build_client_http_server(
        application,
        host=args.host,
        port=args.port,
        static_root=args.static_root,
    )
    print(
        json.dumps(
            {
                "status": "ready",
                "url": f"http://{args.host}:{args.port}",
                "orders_authorized": False,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
