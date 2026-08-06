"""Read-only access to accepted point-in-time snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl

from data_plane.contracts import DatasetSnapshot
from data_plane.storage import sha256_file

SELECTION_SOURCE = "kernel.universe.selection_gates"


@dataclass(frozen=True)
class LoadedSnapshot:
    manifest: DatasetSnapshot
    frame: pl.DataFrame


class SnapshotRepository:
    def __init__(self, data_root: str | Path):
        self.data_root = Path(data_root).resolve()
        self.accepted_root = (self.data_root / "accepted").resolve()

    def _directories(self, source: str) -> list[Path]:
        if not self.accepted_root.exists():
            return []
        directories = sorted(
            self.accepted_root.glob(f"{source}-*"),
            key=lambda item: item.name,
            reverse=True,
        )
        return [item for item in directories if item.is_dir()]

    def _load(self, directory: Path) -> LoadedSnapshot:
        resolved = directory.resolve()
        if not resolved.is_relative_to(self.accepted_root):
            raise ValueError("snapshot path escaped the accepted data root")
        manifest_path = resolved / "manifest.json"
        data_path = resolved / "data.parquet"
        manifest = DatasetSnapshot.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        manifest.assert_usable()
        if manifest.dataset_id != resolved.name:
            raise ValueError("snapshot directory and manifest identity differ")
        if sha256_file(data_path) != manifest.content_sha256:
            raise ValueError(f"snapshot hash mismatch: {manifest.dataset_id}")
        frame = pl.read_parquet(data_path)
        if frame.height != manifest.row_count:
            raise ValueError(f"snapshot row count mismatch: {manifest.dataset_id}")
        return LoadedSnapshot(manifest=manifest, frame=frame)

    def selection_for_date(self, trade_date: date) -> LoadedSnapshot:
        for directory in self._directories(SELECTION_SOURCE):
            loaded = self._load(directory)
            if loaded.manifest.source != SELECTION_SOURCE:
                continue
            if "session_date" not in loaded.frame.columns:
                continue
            filtered = loaded.frame.filter(pl.col("session_date") == trade_date)
            if not filtered.is_empty():
                return LoadedSnapshot(manifest=loaded.manifest, frame=filtered)
        raise LookupError(f"no accepted selection snapshot for {trade_date.isoformat()}")

    def latest_for_source(self, source: str, *, trade_date: date | None = None) -> LoadedSnapshot:
        for directory in self._directories(source):
            loaded = self._load(directory)
            if loaded.manifest.source != source:
                continue
            if trade_date is None:
                return loaded
            date_column = next(
                (
                    column
                    for column in ("session_date", "trade_date", "asof_date")
                    if column in loaded.frame.columns
                ),
                None,
            )
            if date_column is None:
                continue
            filtered = loaded.frame.filter(pl.col(date_column) == trade_date)
            if not filtered.is_empty():
                return LoadedSnapshot(manifest=loaded.manifest, frame=filtered)
        suffix = f" for {trade_date.isoformat()}" if trade_date else ""
        raise LookupError(f"no accepted {source} snapshot{suffix}")

    def history_for_source(
        self,
        source: str,
        *,
        row_limit: int,
    ) -> tuple[LoadedSnapshot, ...]:
        """Load newest accepted single-session snapshots without duplicate sessions."""

        loaded_snapshots = [
            self._load(directory) for directory in self._directories(source)
        ]
        loaded_snapshots.sort(
            key=lambda item: item.manifest.asof_utc,
            reverse=True,
        )
        output: list[LoadedSnapshot] = []
        seen_sessions: set[date] = set()
        row_count = 0
        for loaded in loaded_snapshots:
            if loaded.manifest.source != source:
                continue
            date_column = next(
                (
                    column
                    for column in ("session_date", "trade_date", "asof_date")
                    if column in loaded.frame.columns
                ),
                None,
            )
            if date_column is None:
                continue
            session_values = loaded.frame.get_column(date_column).unique().to_list()
            if len(session_values) != 1 or not isinstance(session_values[0], date):
                continue
            session_date = session_values[0]
            if session_date in seen_sessions:
                continue
            seen_sessions.add(session_date)
            output.append(loaded)
            row_count += loaded.frame.height
            if row_count >= row_limit:
                break
        return tuple(output)

    def selection_row(
        self, trade_date: date, symbol: str
    ) -> tuple[LoadedSnapshot, dict[str, object]]:
        loaded = self.selection_for_date(trade_date)
        normalized = symbol.strip().upper()
        rows = loaded.frame.filter(pl.col("symbol") == normalized).to_dicts()
        if len(rows) != 1:
            raise LookupError(
                f"symbol {normalized!r} is not unique in the accepted selection snapshot"
            )
        return loaded, rows[0]

    def known_symbols(self) -> set[str]:
        symbols: set[str] = set()
        for directory in self._directories(SELECTION_SOURCE):
            loaded = self._load(directory)
            if loaded.manifest.source != SELECTION_SOURCE:
                continue
            symbols.update(
                str(value).upper() for value in loaded.frame.get_column("symbol").to_list()
            )
        return symbols
