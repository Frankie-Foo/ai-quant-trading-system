"""Record or clear the actual manual position watched by monitor_trade_plan."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "runs" / "trade-plan-position.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--flat", action="store_true")
    parser.add_argument("--symbol")
    parser.add_argument("--entry", type=float)
    parser.add_argument("--shares", type=int)
    parser.add_argument("--stop", type=float)
    parser.add_argument("--target-one", type=float)
    parser.add_argument("--target-one-shares", type=int)
    parser.add_argument("--target-two", type=float)
    parser.add_argument("--target-two-shares", type=int)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.flat:
        payload = {
            "active": False,
            "updated_at_utc": datetime.now(UTC).isoformat(),
        }
    else:
        if (
            not args.symbol
            or args.shares is None
            or args.stop is None
        ):
            raise ValueError("symbol, shares and stop are required")
        if (
            args.shares <= 0
            or args.stop <= 0
            or (
                args.entry is not None
                and (args.entry <= 0 or args.stop >= args.entry)
            )
        ):
            raise ValueError("position values are invalid")
        target_pairs = (
            (args.target_one, args.target_one_shares),
            (args.target_two, args.target_two_shares),
        )
        if any((price is None) != (shares is None) for price, shares in target_pairs):
            raise ValueError("each target requires both price and shares")
        targets = [
            {"price": price, "shares": shares}
            for price, shares in target_pairs
            if price is not None and shares is not None
        ]
        if any(
            float(item["price"]) <= args.stop or int(item["shares"]) <= 0
            for item in targets
        ):
            raise ValueError("target values are invalid")
        if sum(int(item["shares"]) for item in targets) > args.shares:
            raise ValueError("target shares exceed the active position")
        payload = {
            "active": True,
            "symbol": str(args.symbol).strip().upper(),
            "entry": args.entry,
            "shares": args.shares,
            "stop": args.stop,
            "targets": targets,
            "updated_at_utc": datetime.now(UTC).isoformat(),
        }
    args.path.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.path.with_suffix(args.path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(args.path)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
