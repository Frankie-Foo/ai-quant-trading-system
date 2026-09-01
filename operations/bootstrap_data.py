"""Verified first-run import of immutable accepted research snapshots."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import polars as pl


class BootstrapImporter:
    def __init__(
        self,
        *,
        archive_path: Path,
        data_root: Path,
        runs_root: Path,
    ):
        self.archive_path = archive_path
        self.data_root = data_root
        self.runs_root = runs_root
        self.marker_path = self.runs_root / "bootstrap-import.json"

    def import_if_needed(self, *, force: bool = False) -> dict[str, object]:
        repaired = self._repair_duplicate_grouped_daily()
        if self.marker_path.is_file() and not force:
            value = json.loads(self.marker_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return {**value, "repaired_duplicate_datasets": repaired}
            return {
                "status": "complete",
                "repaired_duplicate_datasets": repaired,
            }
        if not self.archive_path.is_file():
            return {
                "status": "not_available",
                "imported_datasets": 0,
                "skipped_existing_datasets": 0,
            }
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="aiq-bootstrap-"))
        try:
            files = self._extract_verified(staging)
            datasets = sorted(
                {
                    PurePosixPath(value["path"]).parts[1]
                    for value in files
                }
            )
            accepted = self.data_root / "accepted"
            accepted.mkdir(parents=True, exist_ok=True)
            existing_daily = self._grouped_daily_dates(accepted)
            imported = 0
            skipped = 0
            skipped_semantic = 0
            for dataset in datasets:
                source = staging / "accepted" / dataset
                target = accepted / dataset
                if target.exists():
                    skipped += 1
                    continue
                if dataset.startswith("massive.grouped_daily-"):
                    trade_date = self._snapshot_trade_date(source)
                    if trade_date is not None and trade_date in existing_daily:
                        skipped_semantic += 1
                        continue
                    if trade_date is not None:
                        existing_daily.add(trade_date)
                shutil.move(str(source), str(target))
                imported += 1
            result = {
                "schema_version": "desktop_bootstrap_import.v1",
                "status": "complete",
                "archive": self.archive_path.name,
                "imported_datasets": imported,
                "skipped_existing_datasets": skipped,
                "skipped_semantic_duplicates": skipped_semantic,
                "repaired_duplicate_datasets": repaired,
                "verified_files": len(files),
                "finished_at_utc": datetime.now(UTC).isoformat(),
            }
            temporary = self.marker_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.marker_path)
            return result
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _extract_verified(self, staging: Path) -> list[dict[str, Any]]:
        with zipfile.ZipFile(self.archive_path) as archive:
            try:
                manifest = json.loads(
                    archive.read("bootstrap-manifest.json").decode("utf-8")
                )
            except (KeyError, UnicodeDecodeError, ValueError) as exc:
                raise ValueError("bootstrap manifest is invalid") from exc
            if (
                not isinstance(manifest, dict)
                or manifest.get("schema_version") != "desktop_bootstrap.v1"
                or not isinstance(manifest.get("files"), list)
            ):
                raise ValueError("bootstrap manifest contract is invalid")
            files: list[dict[str, Any]] = []
            for raw in manifest["files"]:
                if not isinstance(raw, dict):
                    raise ValueError("bootstrap file entry is invalid")
                relative = self._safe_relative(raw.get("path"))
                expected_size = int(raw.get("size", -1))
                expected_hash = str(raw.get("sha256", ""))
                payload = archive.read(relative.as_posix())
                if len(payload) != expected_size:
                    raise ValueError("bootstrap file size mismatch")
                if hashlib.sha256(payload).hexdigest() != expected_hash:
                    raise ValueError("bootstrap file hash mismatch")
                target = staging.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                files.append(raw)
            return files

    @staticmethod
    def _safe_relative(value: object) -> PurePosixPath:
        if not isinstance(value, str):
            raise ValueError("unsafe bootstrap path")
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or len(path.parts) < 3
            or path.parts[0] != "accepted"
        ):
            raise ValueError("unsafe bootstrap path")
        return path

    @staticmethod
    def _snapshot_trade_date(dataset: Path) -> object | None:
        path = dataset / "data.parquet"
        try:
            values = pl.read_parquet(path, columns=["trade_date"])[
                "trade_date"
            ].unique().to_list()
        except (OSError, ValueError):
            return None
        return values[0] if len(values) == 1 else None

    def _grouped_daily_dates(self, accepted: Path) -> set[object]:
        return {
            value
            for dataset in accepted.glob("massive.grouped_daily-*")
            if (value := self._snapshot_trade_date(dataset)) is not None
        }

    def _repair_duplicate_grouped_daily(self) -> int:
        accepted = self.data_root / "accepted"
        if not accepted.is_dir():
            return 0
        grouped: dict[object, list[Path]] = {}
        for dataset in accepted.glob("massive.grouped_daily-*"):
            trade_date = self._snapshot_trade_date(dataset)
            if trade_date is not None:
                grouped.setdefault(trade_date, []).append(dataset)
        quarantine = self.data_root / "quarantined" / "duplicate-grouped-daily"
        moved = 0
        for datasets in grouped.values():
            if len(datasets) < 2:
                continue
            keep = max(datasets, key=lambda path: path.stat().st_mtime_ns)
            for dataset in datasets:
                if dataset == keep:
                    continue
                quarantine.mkdir(parents=True, exist_ok=True)
                target = quarantine / dataset.name
                if target.exists():
                    target = quarantine / f"{dataset.name}-{moved}"
                shutil.move(str(dataset), str(target))
                moved += 1
        return moved
