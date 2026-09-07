"""Export verified increments to stdout; explicit Paper GET or pinned offline export."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from operations.local_env import alpaca_paper_credentials
from operations.loop_integration.broker_fills import alpaca_fill_page, collect_broker_fills
from operations.loop_integration.execution_summary import (
    BrokerFillEvidence,
    load_effective_plan,
    read_pinned,
    write_pinned_json,
)


def run(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "review-context", "ledger"):
        parser.add_argument(f"--{name}", type=Path, required=True)
        parser.add_argument(f"--{name}-sha256", required=True)
    parser.add_argument("--trade-date", type=date.fromisoformat, required=True)
    parser.add_argument("--as-of", type=datetime.fromisoformat, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--broker-export", type=Path)
    mode.add_argument("--read-paper-broker", action="store_true")
    parser.add_argument("--broker-export-sha256")
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--metadata-sha256")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    plan = load_effective_plan(
        args.plan, expected_sha256=args.plan_sha256, trade_date=args.trade_date,
        as_of=args.as_of, review_context_path=args.review_context,
        review_context_sha256=args.review_context_sha256, require_native_evidence=True,
    )
    common = dict(plan=plan, ledger_path=args.ledger, ledger_sha256=args.ledger_sha256)
    audit: dict[str, Any] = {"responses": [], "orders_submitted": 0}
    if args.broker_export is not None:
        if not args.broker_export_sha256:
            parser.error("--broker-export-sha256 is required")
        raw = json.loads(read_pinned(args.broker_export, args.broker_export_sha256))
        audit["broker_export_sha256"] = args.broker_export_sha256
        audit["broker_export"] = raw
        metadata = raw["metadata"]
        pages = raw["pages"]

        def page(cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
            matches = [item for item in pages if item["page_token"] == cursor]
            if len(matches) != 1:
                raise ValueError("missing or duplicate broker export page")
            item = matches[0]
            return item["activities"], item["next_page_token"]

        def order(order_id: str) -> dict[str, Any]:
            value = raw["orders"][order_id]
            if not isinstance(value, dict):
                raise ValueError("invalid broker order export")
            return value

        evidence = collect_broker_fills(
            **common, metadata=metadata, read_fill_page=page, read_order=order,
        )
    else:
        if args.metadata is None or args.metadata_sha256 is None:
            parser.error("Paper reads require pinned --metadata and --metadata-sha256")
        metadata = json.loads(read_pinned(args.metadata, args.metadata_sha256))
        validated = BrokerFillEvidence.model_validate({**metadata, "fills": []})
        if (validated.environment != "paper" or validated.broker != "alpaca"
                or validated.coverage_end_utc > args.as_of):
            raise ValueError("Paper metadata environment/as_of mismatch")
        if validated.coverage_end_utc > datetime.now(UTC):
            raise ValueError("future broker coverage cannot be reconciled")
        key, secret = alpaca_paper_credentials(os.environ)
        # No .env discovery, redirects, generic method, order POST, PATCH or DELETE.
        with httpx.Client(
            base_url="https://paper-api.alpaca.markets", timeout=30, follow_redirects=False,
            headers={"APCA-API-KEY-ID": key.get_secret_value(),
                     "APCA-API-SECRET-KEY": secret.get_secret_value()},
        ) as client:
            def get(path: str, params: dict[str, Any]) -> object:
                response = client.get(path, params=params)
                response.raise_for_status()
                value = response.json()
                audit["responses"].append({"path": path, "params": params, "body": value})
                return value

            account = get("/v2/account", {})
            if (not isinstance(account, dict) or account.get("id") != validated.account_id
                    or account.get("currency") != validated.currency):
                raise ValueError("broker account identity/currency mismatch")

            def broker_order(order_id: str) -> dict[str, Any]:
                from urllib.parse import quote

                value = get(f"/v2/orders/{quote(order_id, safe='')}", {"nested": "true"})
                if not isinstance(value, dict):
                    raise ValueError("invalid broker order response")
                return value

            evidence = collect_broker_fills(
                **common, metadata=metadata,
                read_fill_page=lambda cursor: alpaca_fill_page(get, args.trade_date, cursor),
                read_order=broker_order,
            )
    if evidence.coverage_end_utc > args.as_of:
        raise ValueError("broker coverage follows as_of")
    if args.output_dir is None:
        return evidence.model_dump(mode="json")
    audit_path, audit_hash = write_pinned_json(args.output_dir, "broker-raw", audit)
    payload = evidence.model_dump(mode="json")
    fills_path, fills_hash = write_pinned_json(args.output_dir, "broker-fills", payload)
    receipt = {"status": "prepared", "fills_path": str(fills_path), "fills_sha256": fills_hash,
               "raw_audit_path": str(audit_path), "raw_audit_sha256": audit_hash}
    # Audit/account observation changes must not mint a new execution identity.
    receipt_path, receipt_hash = write_pinned_json(args.output_dir, "broker-export", receipt)
    return {**receipt, "receipt_path": str(receipt_path), "receipt_sha256": receipt_hash}


def main() -> None:
    print(json.dumps(run()))


if __name__ == "__main__":
    main()
