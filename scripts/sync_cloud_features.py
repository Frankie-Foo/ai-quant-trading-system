"""Fetch cloud features into the local cache outside the deterministic fast loop."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import SecretStr

from data_plane.cloud_features import CloudFeatureCache, CloudFeatureClient

ROOT = Path(__file__).resolve().parents[1]


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timedelta(0):
        raise argparse.ArgumentTypeError("--asof must be timezone-aware UTC")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--asof", required=True, type=_utc)
    parser.add_argument(
        "--cache-db", type=Path, default=ROOT / "runs/cloud-feature-cache.sqlite3"
    )
    args = parser.parse_args()
    client = CloudFeatureClient(
        base_url=args.base_url,
        token=SecretStr(os.environ["CLOUD_FEATURE_API_TOKEN"]),
    )
    cache = CloudFeatureCache(args.cache_db)
    try:
        for symbol in args.symbols.split(","):
            vector = client.fetch(symbol, asof_utc=args.asof)
            if vector is not None:
                cache.put(vector)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
