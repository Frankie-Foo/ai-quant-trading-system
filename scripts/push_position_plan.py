from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv

from scripts.monitor_trade_plan import (
    DEFAULT_CONFIG,
    DEFAULT_POSITION,
    ROOT,
    Position,
    Signal,
    _send_vps,
    build_position_plan_message,
    load_config,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--position-path", type=Path, default=DEFAULT_POSITION)
    return parser


def _read_position_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("position state must be an object")
    return cast(dict[str, Any], payload)


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = _parser().parse_args()
    config = load_config(args.config)
    payload = _read_position_payload(args.position_path)
    if payload.get("active") is not True:
        raise ValueError("no active position")

    position = Position(
        symbol=str(payload["symbol"]).upper(),
        entry=float(payload["entry"]),
        shares=int(payload["shares"]),
        stop=float(payload["stop"]),
    )
    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list):
        raise ValueError("position targets must be a list")
    targets = tuple(
        (float(target["price"]), int(target["shares"]))
        for target in raw_targets
        if isinstance(target, dict)
    )
    message = build_position_plan_message(
        position,
        account_value=config.account_value,
        targets=targets,
        exit_decision_time_bjt=config.exit_decision_time_bjt,
        force_exit_time_bjt=config.force_exit_time_bjt,
    )
    signal = Signal(
        event="position_plan",
        symbol=position.symbol,
        reason="active_position_percentage_plan",
        message=message,
        dedupe_key=(
            f"position-plan:{position.symbol}:{position.entry}:percentage-utf8"
        ),
    )
    message_id = _send_vps(config.channel_id, signal)
    print(
        json.dumps(
            {
                "ok": True,
                "event": signal.event,
                "symbol": signal.symbol,
                "sender_type": "bot",
                "message_id": message_id,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
