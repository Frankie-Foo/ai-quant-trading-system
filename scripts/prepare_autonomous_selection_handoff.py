"""Create the daily autonomous Paper config and send the initial Livermore plan."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import SecretStr

from operations.autonomous_selection_handoff import prepare_autonomous_selection_handoff
from operations.feishu_base import FeishuBaseEventClient
from operations.livermore_push import LivermorePushClient, configured_identity
from operations.local_env import load_project_env, project_data_root
from schedule.premarket import target_for_tick

ROOT = Path(__file__).resolve().parents[1]


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", type=_parse_date)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    parser.add_argument("--state-root", type=Path, default=ROOT / "runs" / "autonomous")
    parser.add_argument("--max-plans", type=int, default=5)
    return parser


def main() -> int:
    load_project_env(ROOT)
    args = _parser().parse_args()
    trade_date = args.trade_date or target_for_tick(datetime.now(UTC))
    if trade_date is None:
        raise RuntimeError("no due XNYS trade date for autonomous handoff")
    day_root = args.state_root / trade_date.isoformat()
    app_id, channel_id = configured_identity(os.environ)
    push = LivermorePushClient(
        app_id=app_id,
        app_secret=SecretStr(os.getenv("VPS_LIVERMORE_APP_SECRET", "")),
        channel_id=channel_id,
    )
    try:
        receipt = prepare_autonomous_selection_handoff(
            data_root=args.data_root,
            trade_date=trade_date,
            output_path=day_root / "autonomous_paper.json",
            confirmation_path=day_root / "open_confirmation.json",
            notification_db=day_root / "notifications.sqlite3",
            push=push,
            audit=FeishuBaseEventClient.from_environment(os.environ),
            max_plans=args.max_plans,
        )
    finally:
        push.close()
    print(
        json.dumps(
            {
                "ok": True,
                "trade_date": trade_date.isoformat(),
                "config_path": str(receipt.config_path),
                "symbols": list(receipt.symbols),
                "selection_snapshot_id": receipt.selection_snapshot_id,
                "message_id": receipt.message_id,
                "open_confirmation_id": (
                    receipt.authorization.open_confirmation_id
                    if receipt.authorization is not None
                    else None
                ),
                "orders_submitted": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
