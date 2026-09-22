"""Explicit read-only Alpaca news ingestion; no scheduler or default credential path."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from dotenv import dotenv_values
from pydantic import SecretStr

from data_plane.event_ledger import EventLedger
from data_plane.providers.alpaca_direct import DirectAlpacaMarketDataClient
from data_plane.providers.catalyst_news import fetch_alpaca_news_direct, ingest_catalyst_events
from operations.local_env import sip_monitoring_window


def _guard_request(request: httpx.Request) -> None:
    if not sip_monitoring_window(datetime.now(UTC)):
        raise PermissionError("Alpaca news ingestion is allowed only during the monitoring window")
    if (
        request.method != "GET"
        or request.url.scheme != "https"
        or request.url.host != "data.alpaca.markets"
        or request.url.port not in (None, 443)
        or request.url.path != "/v1beta1/news"
    ):
        raise PermissionError("only the allowlisted Alpaca read-only news endpoint is permitted")


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone required")
        return parsed.astimezone(UTC)
    except ValueError:
        raise argparse.ArgumentTypeError("provide a timezone-aware ISO timestamp") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--symbols", required=True, nargs="+", help="user/discovery universe")
    parser.add_argument("--origin", choices=("historical", "forward"), default="historical")
    parser.add_argument("--start-utc", type=_parse_time)
    parser.add_argument("--end-utc", type=_parse_time)
    parser.add_argument("--lookback-minutes", type=int, default=15)
    parser.add_argument("--data-url", help="only https://data.alpaca.markets is allowed")
    args = parser.parse_args()
    try:
        if not sip_monitoring_window(datetime.now(UTC)):
            raise PermissionError(
                "Alpaca news ingestion is allowed only during the monitoring window"
            )
        origin: Literal["historical", "forward"] = args.origin
        now = datetime.now(UTC)
        if origin == "forward":
            if args.start_utc is not None or args.end_utc is not None:
                raise ValueError("forward ingestion cannot accept a historical time override")
            if not 1 <= args.lookback_minutes <= 60:
                raise ValueError("forward lookback must be between 1 and 60 minutes")
            start, end = now - timedelta(minutes=args.lookback_minutes), now
        else:
            if args.start_utc is None or args.end_utc is None:
                raise ValueError("historical ingestion requires start and end timestamps")
            start, end = args.start_utc, args.end_utc
            if end > now or end <= start:
                raise ValueError("historical ingestion requires start < end <= now")
        symbols = tuple(sorted({item.strip().upper() for item in args.symbols}))
        if not symbols or any(
            not item or len(item) > 15 or not item[0].isalpha()
            or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for char in item)
            for item in symbols
        ):
            raise ValueError("symbols must be explicit valid Alpaca symbols")
        if not args.env_file.is_file():
            raise ValueError("explicit market-data credential file is missing")
        # No auto discovery, interpolation, os.environ mutation, or Paper aliases.
        values = dotenv_values(args.env_file, interpolate=False)
        key_id = (values.get("ALPACA_API_KEY") or "").strip()
        secret = (values.get("ALPACA_SECRET_KEY") or "").strip()
        if not key_id or not secret:
            raise ValueError("market-data credential file requires the complete key pair")
        data_url = (
            args.data_url or values.get("ALPACA_DATA_URL")
            or DirectAlpacaMarketDataClient.DATA_BASE_URL
        ).strip().rstrip("/")
        if data_url != DirectAlpacaMarketDataClient.DATA_BASE_URL:
            raise ValueError("market-data URL must be the allowlisted HTTPS Alpaca data host")
        with httpx.Client(
            timeout=60.0, follow_redirects=False, trust_env=False,
            event_hooks={"request": [_guard_request]},
        ) as http_client:
            client = DirectAlpacaMarketDataClient(
                key_id=SecretStr(key_id), secret_key=SecretStr(secret),
                base_url=data_url, client=http_client,
            )
            try:
                frame = fetch_alpaca_news_direct(start, end, symbols=symbols, client=client)
            finally:
                client.close()
        # Only a completely successful request batch may open/persist the ledger.
        args.ledger.parent.mkdir(parents=True, exist_ok=True)
        with EventLedger(args.ledger) as ledger:
            revisions = ingest_catalyst_events(frame, ledger=ledger, origin=origin)
        print(json.dumps({
            "status": "ingested" if revisions else "empty_response",
            "origin": origin,
            "observations": len(revisions),
            "requested_symbols": len(symbols),
            "text_scope": "provider_headline_summary",
        }))
        return 0
    except Exception as exc:
        # Provider/input exceptions may contain credential-bearing values or text.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
