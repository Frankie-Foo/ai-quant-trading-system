"""Serve the self-contained macOS local research runtime on loopback."""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from execution.ibkr_execution import BrokerPort
from execution.ibkr_execution import ExecutionDesk as IbkrExecutionDesk
from execution.ibkr_tws_adapter import OfficialIbapiAdapter
from operations.alpaca_paper_autopilot import AlpacaPaperAutopilot
from operations.bootstrap_data import BootstrapImporter
from operations.desktop_paper_safety import DesktopPaperSafetyRefresher
from operations.local_research_http import build_local_research_http_server
from operations.local_research_runtime import (
    AlpacaProxyMarketDataAdapter,
    DisabledExecutionDesk,
    EnvironmentMarketDataAdapter,
    LocalResearchRuntime,
    MarketDataAdapter,
    StandaloneMarketDataAdapter,
    UnconfiguredMarketDataAdapter,
)

IBKR_REQUIRED_CONFIGURATION_KEYS = (
    "IBKR_HOST",
    "IBKR_CLIENT_ID",
    "IBKR_MAX_ORDER_NOTIONAL",
)


def _build_execution_desk(
    *,
    environ: Mapping[str, str],
    runs_root: Path,
    broker_factory: Callable[[], BrokerPort] | None = None,
) -> IbkrExecutionDesk | DisabledExecutionDesk:
    """Build a live-only desk without ever accepting broker credentials.

    An incomplete profile is deliberately treated as disabled.  A complete but
    malformed profile is rejected so the desktop cannot silently connect with a
    different account or risk limit.  The broker port is fixed inside the desk at
    4001 and cannot be supplied by the renderer or environment.
    """

    values = {
        key: str(environ.get(key, "")).strip()
        for key in (*IBKR_REQUIRED_CONFIGURATION_KEYS, "IBKR_LIVE_ACCOUNT")
    }
    if any(not values[key] for key in IBKR_REQUIRED_CONFIGURATION_KEYS):
        return DisabledExecutionDesk()
    host = values["IBKR_HOST"]
    if len(host) > 253 or re.fullmatch(r"[A-Za-z0-9.-]+", host) is None:
        raise ValueError("IBKR host is invalid")
    client_id_text = values["IBKR_CLIENT_ID"]
    if re.fullmatch(r"\d+", client_id_text) is None:
        raise ValueError("IBKR client id is invalid")
    client_id = int(client_id_text)
    if client_id > 2_147_483_647:
        raise ValueError("IBKR client id is invalid")
    live_account = values["IBKR_LIVE_ACCOUNT"].upper()
    if live_account and re.fullmatch(r"[A-Z0-9-]{4,32}", live_account) is None:
        raise ValueError("IBKR live account is invalid")
    try:
        max_notional = Decimal(values["IBKR_MAX_ORDER_NOTIONAL"])
    except InvalidOperation as exc:
        raise ValueError("IBKR max order notional is invalid") from exc
    if not max_notional.is_finite() or max_notional <= 0:
        raise ValueError("IBKR max order notional is invalid")
    broker = (
        broker_factory()
        if broker_factory is not None
        else OfficialIbapiAdapter(
            api_read_only=False,
            expected_account_id=live_account or None,
        )
    )
    return IbkrExecutionDesk(
        runs_root / "ibkr-execution.sqlite3",
        broker,
        live_account=live_account or None,
        host=host,
        client_id=client_id,
        max_notional=max_notional,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--bootstrap-archive", type=Path)
    parser.add_argument(
        "--provider-id",
        choices=("unconfigured", "environment", "alpaca_proxy", "standalone"),
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
    elif args.provider_id == "standalone":
        market_data = StandaloneMarketDataAdapter()
    else:
        market_data = UnconfiguredMarketDataAdapter()
    data_root = args.data_root.expanduser().resolve()
    runs_root = args.runs_root.expanduser().resolve()
    bootstrap_status = None
    if args.bootstrap_archive is not None:
        bootstrap_status = BootstrapImporter(
            archive_path=args.bootstrap_archive.expanduser().resolve(),
            data_root=data_root,
            runs_root=runs_root,
        ).import_if_needed()
    runtime = LocalResearchRuntime(
        data_root=data_root,
        runs_root=runs_root,
        market_data=market_data,
        bootstrap_status=bootstrap_status,
        execution_desk=_build_execution_desk(
            environ=os.environ,
            runs_root=runs_root,
        ),
        paper_autopilot=AlpacaPaperAutopilot(
            data_root=data_root,
            runs_root=runs_root,
            environ=os.environ,
            safety_refresher=DesktopPaperSafetyRefresher(
                runs_root=runs_root,
                environ=os.environ,
            ),
        ),
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
