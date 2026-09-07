"""Immutable pre-write startup observations, not declarations of daily completeness."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

from execution.alpaca_paper import BrokerOrder, PaperAccount, PaperPosition


def capture_startup(
    *, directory: Path, trade_date: date, account: PaperAccount,
    positions: tuple[PaperPosition, ...], open_orders: tuple[BrokerOrder, ...],
    plan_path: Path, confirmation_path: Path, ledger_path: Path,
    observed_start_utc: datetime, observed_end_utc: datetime,
) -> Path:
    for stamp in (observed_start_utc, observed_end_utc):
        if stamp.tzinfo is None or stamp.utcoffset() != UTC.utcoffset(stamp):
            raise ValueError("startup observations require UTC")
    if observed_end_utc < observed_start_utc:
        raise ValueError("startup observation window is reversed")
    payload = {
        "schema_version": "paper_run_startup.v1", "trade_date": str(trade_date),
        "broker": "alpaca", "environment": "paper",
        "broker_base_url": "https://paper-api.alpaca.markets",
        "observed_start_utc": observed_start_utc.isoformat(),
        "observed_end_utc": observed_end_utc.isoformat(),
        "account": account.model_dump(mode="json"),
        "positions": [item.model_dump(mode="json") for item in positions],
        "open_orders": [item.model_dump(mode="json") for item in open_orders],
        "ledger_path": str(ledger_path.resolve()),
        "plan_path": str(plan_path.resolve()),
        "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "confirmation_path": str(confirmation_path.resolve()),
        "confirmation_sha256": hashlib.sha256(confirmation_path.read_bytes()).hexdigest(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    digest = hashlib.sha256(raw).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"startup-{digest}.json"
    try:
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if path.read_bytes() != raw:
            raise ValueError("startup evidence hash mismatch") from None
    return path
