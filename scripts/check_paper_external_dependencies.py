"""Read-only health check for Alpaca Paper, Investment Base and Livermore."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import SecretStr

from execution.alpaca_paper import DirectAlpacaPaperBroker
from operations.feishu_base import FeishuBaseEventClient
from operations.livermore_push import LivermorePushClient, configured_identity
from operations.local_env import load_project_env

ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    load_project_env(ROOT)
    if args.env_file is not None:
        if not args.env_file.is_file():
            raise FileNotFoundError("external environment file does not exist")
        load_dotenv(args.env_file, override=False)
        load_project_env(ROOT)

    broker = DirectAlpacaPaperBroker(
        key_id=SecretStr(os.getenv("ALPACA_PAPER_KEY_ID", "")),
        secret_key=SecretStr(os.getenv("ALPACA_PAPER_SECRET_KEY", "")),
        writes_enabled=False,
    )
    try:
        account = broker.get_account()
    finally:
        broker.close()
    if account.status != "ACTIVE" or account.account_blocked or account.trading_blocked:
        raise RuntimeError("Alpaca Paper account is not active and tradable")

    feishu = FeishuBaseEventClient.from_environment(os.environ)
    if feishu is None:
        raise RuntimeError("dedicated Investment Base is not configured")
    checked_tables = feishu.check_access()

    app_id, channel_id = configured_identity(os.environ)
    livermore = LivermorePushClient(
        app_id=app_id,
        app_secret=SecretStr(os.getenv("VPS_LIVERMORE_APP_SECRET", "")),
        channel_id=channel_id,
    )
    try:
        channel_available = livermore.configured_channel_available()
    finally:
        livermore.close()
    if not channel_available:
        raise RuntimeError("configured Livermore channel is unavailable")

    print(
        json.dumps(
            {
                "ok": True,
                "alpaca_broker": broker.broker_identity,
                "alpaca_base_url": broker.base_url,
                "account_status": account.status,
                "feishu_tables_checked": sorted(checked_tables),
                "livermore_app_id": app_id,
                "livermore_channel_id": channel_id,
                "orders_submitted": 0,
                "records_written": 0,
                "messages_sent": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
