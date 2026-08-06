"""Build a verified desktop bootstrap archive from accepted snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

PREFIXES = (
    "massive.grouped_daily-",
    "massive.news.history-",
    "massive.reference_tickers.cs-",
    "massive.ticker_details-",
    "massive.free_float-",
    "kernel.universe.selection_gates-",
    "research.intraday_selection_postmortem-",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    args = _parser().parse_args()
    source_root = args.source_data_root.expanduser().resolve()
    accepted = source_root / "accepted"
    if not accepted.is_dir():
        raise FileNotFoundError(f"accepted data root is missing: {accepted}")
    datasets = sorted(
        path
        for path in accepted.iterdir()
        if path.is_dir() and path.name.startswith(PREFIXES)
    )
    files: list[dict[str, object]] = []
    for dataset in datasets:
        for path in sorted(value for value in dataset.rglob("*") if value.is_file()):
            relative = path.relative_to(source_root).as_posix()
            files.append(
                {
                    "path": relative,
                    "size": path.stat().st_size,
                    "sha256": _digest(path),
                }
            )
    manifest = {
        "schema_version": "desktop_bootstrap.v1",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "dataset_count": len(datasets),
        "file_count": len(files),
        "prefixes": list(PREFIXES),
        "files": files,
    }
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    with zipfile.ZipFile(
        temporary,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        archive.writestr(
            "bootstrap-manifest.json",
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
        )
        for value in files:
            relative = str(value["path"])
            archive.write(source_root / relative, relative)
    temporary.replace(output)
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(output),
                "dataset_count": len(datasets),
                "file_count": len(files),
                "archive_bytes": output.stat().st_size,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
