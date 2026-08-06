"""Read-only verification of the keyless cloud market/Paper platform API."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from execution.alpaca_paper import CloudPaperBroker
from execution.alpaca_sip_stream import PlatformSipStream
from execution.settings import ExecutionSettings
from operations.evidence import (
    load_existing_evidence,
    refresh_maturity_evidence,
    write_evidence_atomic,
)
from operations.readiness import Attestation
from schedule.runtime import ProcessLock

ROOT = Path(__file__).resolve().parents[1]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="AAPL", help="Comma-separated probe symbols")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--lock-file", type=Path, default=Path("runs/alpaca-sip.lock"))
    parser.add_argument(
        "--receipt", type=Path, default=ROOT / "runs/alpaca-access-receipt.json"
    )
    parser.add_argument(
        "--evidence", type=Path, default=ROOT / "runs/maturity-evidence.json"
    )
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument(
        "--order-db", type=Path, default=ROOT / "runs/paper-orders.sqlite3"
    )
    return parser.parse_args()


async def _verify(args: argparse.Namespace) -> dict[str, object]:
    settings = ExecutionSettings()  # type: ignore[call-arg]
    symbols = tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip())
    broker = CloudPaperBroker(
        base_url=settings.cloud_platform_base_url,
        token=settings.cloud_paper_api_token,
        writes_enabled=False,
    )
    try:
        account = broker.get_account()
    finally:
        broker.close()
    stream = PlatformSipStream(
        base_url=settings.cloud_platform_base_url,
        token=settings.cloud_market_data_api_token,
        symbols=symbols,
    )
    subscription = await stream.probe(timeout_seconds=args.timeout_seconds)
    return {
        "status": "ok",
        "orders_submitted": 0,
        "paper_account": {
            "status": account.status,
            "account_blocked": account.account_blocked,
            "trading_blocked": account.trading_blocked,
        },
        "sip_stream": {
            "connected": subscription.connected,
            "authenticated": subscription.authenticated,
            "bars": list(subscription.bars),
            "quotes": list(subscription.quotes),
        },
    }


def main() -> int:
    args = _parse_args()
    with ProcessLock(args.lock_file):
        result = asyncio.run(_verify(args))
    result["verified_at_utc"] = datetime.now(UTC).isoformat()
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.receipt.with_suffix(f"{args.receipt.suffix}.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(args.receipt)
    paper = result["paper_account"]
    sip = result["sip_stream"]
    if not isinstance(paper, dict) or not isinstance(sip, dict):
        raise RuntimeError("Alpaca verification result is malformed")
    paper_passed = (
        str(paper.get("status", "")).upper() == "ACTIVE"
        and paper.get("account_blocked") is False
        and paper.get("trading_blocked") is False
    )
    sip_passed = (
        sip.get("connected") is True
        and sip.get("authenticated") is True
        and bool(sip.get("bars"))
        and bool(sip.get("quotes"))
    )
    evidence = refresh_maturity_evidence(
        data_root=args.data_root,
        order_db=args.order_db,
        existing=load_existing_evidence(args.evidence),
    )
    reference = f"access-receipt:{args.receipt}"
    evidence = evidence.model_copy(
        update={
            "full_market_realtime_data": Attestation(
                passed=sip_passed,
                evidence_refs=(reference,) if sip_passed else (),
            ),
            "paper_broker_access": Attestation(
                passed=paper_passed,
                evidence_refs=(reference,) if paper_passed else (),
            ),
        }
    )
    write_evidence_atomic(evidence, args.evidence)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if paper_passed and sip_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
