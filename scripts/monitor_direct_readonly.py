"""Direct-Alpaca, event-only watcher for a single read-only trade plan."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import SecretStr

from data_plane.providers.alpaca_direct import DirectAlpacaMarketDataClient
from operations.livermore_push import LivermorePushClient
from operations.local_env import load_project_env

ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--entry", type=float, required=True)
    parser.add_argument("--stop", type=float, required=True)
    parser.add_argument("--tp1", type=float, required=True)
    parser.add_argument("--tp2", type=float, required=True)
    parser.add_argument("--channel-id", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=int, default=18_000)
    return parser


def _load_state(path: Path) -> dict[str, bool]:
    if not path.exists():
        return {"entry": False, "stop": False, "tp1": False, "tp2": False}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(path: Path, state: dict[str, bool]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def _message(event: str, symbol: str, bid: float, args: argparse.Namespace) -> str:
    text = {
        "entry": "\u3010\u5df4\u83f2\u7279\u4e28\u5b9e\u76d8\u53ea\u8bfb\u3011{symbol}\u8d85\u8fc7\u5165\u573a\u89e6\u53d1 ${entry:.2f}\uff0c\u6700\u65b0\u4e70\u4e00 ${bid:.2f}\u3002\u4ec5\u63d0\u793a\uff0c\u4e0d\u4e0b\u5b9e\u76d8\u5355\u3002",
        "stop": "\u3010\u5df4\u83f2\u7279\u4e28\u5b9e\u76d8\u53ea\u8bfb\u3011{symbol}\u8dcc\u7834\u6b62\u635f ${stop:.2f}\uff0c\u6700\u65b0\u4e70\u4e00 ${bid:.2f}\u3002\u5982\u5df2\u6301\u4ed3\uff0c\u5e94\u9000\u51fa\u3002",
        "tp1": "\u3010\u5df4\u83f2\u7279\u4e28\u5b9e\u76d8\u53ea\u8bfb\u3011{symbol}\u89e6\u53ca TP1 ${tp1:.2f}\uff0c\u6700\u65b0\u4e70\u4e00 ${bid:.2f}\u3002\u5982\u5df2\u6301\u4ed3\uff0c\u53ef\u6267\u884c\u7b2c\u4e00\u6863\u51cf\u4ed3\u3002",
        "tp2": "\u3010\u5df4\u83f2\u7279\u4e28\u5b9e\u76d8\u53ea\u8bfb\u3011{symbol}\u89e6\u53ca TP2 ${tp2:.2f}\uff0c\u6700\u65b0\u4e70\u4e00 ${bid:.2f}\u3002\u5982\u5df2\u6301\u4ed3\uff0c\u53ef\u6267\u884c\u7b2c\u4e8c\u6863\u51cf\u4ed3\u3002",
    }[event]
    return text.format(symbol=symbol, bid=bid, entry=args.entry, stop=args.stop, tp1=args.tp1, tp2=args.tp2)


def main() -> int:
    args = _parser().parse_args()
    if not args.stop < args.entry < args.tp1 < args.tp2:
        raise ValueError("plan prices must satisfy stop < entry < tp1 < tp2")
    load_project_env(ROOT)
    symbol = args.symbol.strip().upper()
    state = _load_state(args.state)
    client = DirectAlpacaMarketDataClient(
        key_id=SecretStr(os.environ["ALPACA_API_KEY_ID"]),
        secret_key=SecretStr(os.environ["ALPACA_API_SECRET_KEY"]),
    )
    push = LivermorePushClient(
        app_id=os.environ["VPS_BUFFETT_APP_ID"],
        app_secret=SecretStr(os.environ["VPS_BUFFETT_APP_SECRET"]),
        channel_id=args.channel_id,
    )
    deadline = time.monotonic() + args.duration_seconds
    try:
        while time.monotonic() < deadline:
            started = time.monotonic()
            now = datetime.now(UTC)
            quotes = client.fetch_quotes((symbol,), start_utc=now - timedelta(seconds=5), end_utc=now)
            if quotes:
                bid = quotes[-1].bid_price
                event = (
                    "stop" if state["entry"] and not state["stop"] and bid <= args.stop else
                    "tp2" if state["entry"] and not state["tp2"] and bid >= args.tp2 else
                    "tp1" if state["entry"] and not state["tp1"] and bid >= args.tp1 else
                    "entry" if not state["entry"] and bid >= args.entry else None
                )
                if event:
                    body = _message(event, symbol, bid, args)
                    if "?" in body or "\ufffd" in body:
                        raise RuntimeError("outgoing message encoding validation failed")
                    push.push(body)
                    state[event] = True
                    _save_state(args.state, state)
                    args.log.parent.mkdir(parents=True, exist_ok=True)
                    with args.log.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps({"event": event, "bid": bid, "ts_utc": now.isoformat()}) + "\n")
            time.sleep(max(0.0, 1.0 - (time.monotonic() - started)))
    finally:
        client.close()
        push.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
