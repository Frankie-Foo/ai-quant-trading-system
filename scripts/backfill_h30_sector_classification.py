"""Fetch point-in-time SIC classifications for triggered H30 candidates."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.http import DownloadError, get_json
from data_plane.providers.massive import TICKER_REFERENCE_URL, api_key_from_env
from data_plane.storage import persist_snapshot
from operations.local_env import load_project_env
from research.h30_challenger import sector_proxy_from_sic

ROOT = Path(__file__).resolve().parents[1]
LABEL_SOURCE = "research.h30_challenger.labels"
SOURCE = "research.h30_sector_classification"
PACE_SECONDS = 12.5


def _manifest(path: Path) -> DatasetSnapshot:
    return DatasetSnapshot.model_validate_json(
        (path.parent / "manifest.json").read_text(encoding="utf-8")
    )


def _latest_labels(data_root: Path) -> tuple[pl.DataFrame, DatasetSnapshot]:
    matches = [
        (_manifest(path).asof_utc, path, _manifest(path))
        for path in (data_root / "accepted").glob(f"{LABEL_SOURCE}-*/data.parquet")
    ]
    if not matches:
        raise FileNotFoundError("H30 labels are missing")
    _, path, snapshot = max(matches, key=lambda item: item[0])
    return pl.read_parquet(path), snapshot


def _check(
    name: str,
    passed: bool,
    observed: object,
    expected: str,
    *,
    severity: QualitySeverity = QualitySeverity.CRITICAL,
) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=severity,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance="scripts.backfill_h30_sector_classification.v1",
    )


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", type=Path, default=ROOT / "runtime" / "ai-quant" / "data"
    )
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    labels, parent = _latest_labels(args.data_root)
    targets = (
        labels.group_by("symbol")
        .agg(pl.col("trade_date").min().alias("asof_date"))
        .sort("symbol")
    )
    if args.plan_only:
        print(json.dumps({"symbols": targets.height, "requests": targets.height}))
        return
    key = api_key_from_env()
    headers = {"Authorization": f"Bearer {key}"}
    rows: list[dict[str, object]] = []
    previous_started = 0.0
    for index, target in enumerate(targets.iter_rows(named=True), start=1):
        elapsed = time.monotonic() - previous_started
        if previous_started and elapsed < PACE_SECONDS:
            time.sleep(PACE_SECONDS - elapsed)
        previous_started = time.monotonic()
        symbol = str(target["symbol"])
        asof_date = target["asof_date"]
        error: str | None = None
        try:
            payload = get_json(
                f"{TICKER_REFERENCE_URL}/{symbol}",
                params={"date": asof_date.isoformat()},
                headers=headers,
                attempts=1,
                timeout_seconds=10,
            )
            value = payload.get("results")
            details = value if isinstance(value, dict) else {}
        except DownloadError as exc:
            details = {}
            error = type(exc).__name__
        sic_code = str(details.get("sic_code") or "")
        rows.append(
            {
                "symbol": symbol,
                "asof_date": asof_date,
                "sic_code": sic_code or None,
                "sic_description": details.get("sic_description"),
                "sector_proxy": sector_proxy_from_sic(sic_code),
                "provider_last_updated_utc": details.get("last_updated_utc"),
                "retrieved_utc": datetime.now(UTC),
                "fetch_error": error,
                "provenance": f"massive.ticker_details:{symbol}@{asof_date.isoformat()}",
            }
        )
        print(json.dumps({"completed": index, "total": targets.height, "symbol": symbol}))
    frame = pl.DataFrame(rows).with_columns(
        pl.col("asof_date").cast(pl.Date),
        pl.col("provider_last_updated_utc")
        .cast(pl.String)
        .str.to_datetime(time_zone="UTC", strict=False),
        pl.col("retrieved_utc").cast(pl.Datetime("ms", "UTC")),
    )
    missing_sic = frame.filter(pl.col("sic_code").is_null()).height
    unmapped = frame.filter(pl.col("sector_proxy").is_null()).height
    snapshot, path = persist_snapshot(
        frame,
        root=args.data_root,
        source=SOURCE,
        schema_version="h30_sector_classification.v1",
        checks=(
            _check("non_empty", frame.height > 0, frame.height, ">0"),
            _check(
                "unique_symbol",
                frame.get_column("symbol").n_unique() == frame.height,
                frame.get_column("symbol").n_unique(),
                str(frame.height),
            ),
            _check(
                "provider_sic_coverage",
                missing_sic == 0,
                missing_sic,
                "0",
                severity=QualitySeverity.WARNING,
            ),
            _check(
                "sector_proxy_coverage",
                unmapped == 0,
                unmapped,
                "0",
                severity=QualitySeverity.WARNING,
            ),
        ),
        parent_snapshot_ids=(parent.dataset_id,),
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "rows": frame.height,
                "missing_sic": missing_sic,
                "unmapped": unmapped,
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
            }
        )
    )


if __name__ == "__main__":
    main()
