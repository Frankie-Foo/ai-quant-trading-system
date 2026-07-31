"""Serve the self-contained macOS local research runtime on loopback."""

from __future__ import annotations

import argparse
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from operations.local_research_http import build_local_research_http_server
from operations.local_research_runtime import (
    AlpacaProxyMarketDataAdapter,
    EnvironmentMarketDataAdapter,
    LocalResearchRuntime,
    MarketDataAdapter,
    UnconfiguredMarketDataAdapter,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument(
        "--provider-id",
        choices=("unconfigured", "environment", "alpaca_proxy"),
        default="unconfigured",
    )
    parser.add_argument(
        "--bearer-token-env",
        default="MACOS_RESEARCH_RUNTIME_TOKEN",
    )
    parser.add_argument("--tick-seconds", type=int, default=60)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 15 <= args.tick_seconds <= 3600:
        raise ValueError("tick-seconds must be between 15 and 3600")
    token = os.environ.get(args.bearer_token_env, "").strip()
    if not token:
        raise ValueError("ephemeral local runtime token is missing")
    market_data: MarketDataAdapter
    if args.provider_id == "environment":
        market_data = EnvironmentMarketDataAdapter()
    elif args.provider_id == "alpaca_proxy":
        market_data = AlpacaProxyMarketDataAdapter()
    else:
        market_data = UnconfiguredMarketDataAdapter()
    runtime = LocalResearchRuntime(
        data_root=args.data_root.expanduser().resolve(),
        runs_root=args.runs_root.expanduser().resolve(),
        market_data=market_data,
    )
    server = build_local_research_http_server(
        runtime,
        host=args.host,
        port=args.port,
        bearer_token=token,
    )
    stop = threading.Event()

    def run_scheduler() -> None:
        while not stop.is_set():
            try:
                runtime.run_due(datetime.now(UTC))
            except Exception:
                # JobLedger and the immutable data store retain the diagnosable failure.
                # The desktop host stays alive so users can inspect evidence/status.
                pass
            stop.wait(args.tick_seconds)

    scheduler = threading.Thread(
        target=run_scheduler,
        name="macos-local-research-scheduler",
        daemon=True,
    )
    scheduler.start()
    print(
        json.dumps(
            {
                "schema_version": "macos_local_research_handshake.v1",
                "url": f"http://127.0.0.1:{server.server_port}",
                "local_execution": True,
                "provider_id": args.provider_id,
                "orders_authorized": False,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        scheduler.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
