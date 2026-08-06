from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from operations.backup import create_backup, prune_backups, restore_and_verify

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--state-root", type=Path, default=ROOT / "runs")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--include-data", action="store_true")
    parser.add_argument("--retention-days", type=int, default=90)
    parser.add_argument("--keep-last", type=int, default=4)
    args = parser.parse_args()
    sqlite_paths = tuple(sorted(args.state_root.glob("*.sqlite3")))
    archive = create_backup(
        data_root=args.data_root,
        sqlite_paths=sqlite_paths,
        destination_dir=args.destination,
        include_data=args.include_data,
    )
    with tempfile.TemporaryDirectory(prefix="trading-restore-drill-") as temporary:
        verified = restore_and_verify(archive, restore_dir=Path(temporary))
    removed = prune_backups(
        args.destination,
        retention_days=args.retention_days,
        keep_last=args.keep_last,
    )
    print(
        json.dumps(
            {
                "status": "complete",
                "archive": str(archive),
                "verified_files": len(verified),
                "include_data": args.include_data,
                "expired_archives_removed": len(removed),
            }
        )
    )


if __name__ == "__main__":
    main()
