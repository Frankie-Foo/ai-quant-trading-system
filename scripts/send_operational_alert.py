from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from operations.alerts import WebhookAlerter

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", required=True)
    parser.add_argument("--severity", choices=("warning", "critical"), default="critical")
    parser.add_argument("--service", required=True)
    parser.add_argument(
        "--receipt-log", type=Path, default=ROOT / "runs/alert-receipts.jsonl"
    )
    args = parser.parse_args()
    webhook = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if not webhook:
        raise RuntimeError("ALERT_WEBHOOK_URL is not configured")
    alerter = WebhookAlerter(webhook_url=webhook, receipt_log=args.receipt_log)
    try:
        receipt = alerter.send(
            alert_id=f"{args.service}-{uuid4().hex}",
            severity=args.severity,
            summary=args.summary,
            details={"service": args.service},
        )
    finally:
        alerter.close()
    print(
        json.dumps(
            {
                "delivered": receipt.delivered,
                "acknowledgement_id": receipt.acknowledgement_id,
                "channel": receipt.channel,
            }
        )
    )


if __name__ == "__main__":
    main()
