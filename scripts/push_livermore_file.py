from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from scripts.monitor_trade_plan import ROOT, Signal, _send_vps, load_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("message_path", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--event", default="research_review")
    parser.add_argument("--symbol", default="MARKET")
    parser.add_argument("--reason", default="scheduled_review")
    return parser


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = _parser().parse_args()
    config = load_config(args.config)
    body = args.message_path.read_text(encoding="utf-8").strip()
    if not body:
        raise ValueError("message file is empty")
    signal = Signal(
        event=args.event,
        symbol=str(args.symbol).upper(),
        reason=args.reason,
        message=body,
        dedupe_key=f"{args.event}:{config.trade_date}:{args.reason}",
    )
    message_id = _send_vps(config.channel_id, signal)
    print(
        json.dumps(
            {
                "ok": True,
                "event": signal.event,
                "sender_type": "bot",
                "message_id": message_id,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
