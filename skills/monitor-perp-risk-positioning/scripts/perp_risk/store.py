"""SQLite state, evidence lineage, outcomes, and configuration candidates."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import OutcomeRecord, PerpObservation, RiskSnapshot
from .positioning import PositionState


class RiskStore:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._initialize()

    def close(self) -> None:
        self._connection.close()

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS observations (
                snapshot_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                market TEXT NOT NULL,
                instrument TEXT NOT NULL,
                observed_at_utc TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (
                    snapshot_id,
                    venue,
                    market,
                    instrument
                )
            );
            CREATE INDEX IF NOT EXISTS observations_lookup
                ON observations (
                    venue,
                    market,
                    instrument,
                    observed_at_utc DESC
                );

            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id TEXT PRIMARY KEY,
                asof_utc TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS snapshots_asof
                ON snapshots (asof_utc DESC);

            CREATE TABLE IF NOT EXISTS position_states (
                target_id TEXT PRIMARY KEY,
                effective_multiplier REAL NOT NULL,
                pending_multiplier REAL NOT NULL,
                pending_windows INTEGER NOT NULL,
                last_window_id INTEGER NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS outcomes (
                outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                observed_at_utc TEXT NOT NULL,
                horizon_minutes INTEGER NOT NULL,
                return_pct REAL NOT NULL,
                payload_json TEXT NOT NULL,
                recorded_at_utc TEXT NOT NULL,
                UNIQUE (
                    snapshot_id,
                    target_id,
                    kind,
                    observed_at_utc,
                    horizon_minutes
                )
            );

            CREATE TABLE IF NOT EXISTS config_candidates (
                candidate_hash TEXT PRIMARY KEY,
                created_at_utc TEXT NOT NULL,
                status TEXT NOT NULL,
                source_config_hash TEXT NOT NULL,
                candidate_yaml TEXT NOT NULL,
                report_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._connection.commit()

    def previous_observations(
        self,
        keys: Iterable[tuple[str, str, str]],
        *,
        before_utc: datetime,
        max_gap_seconds: int,
    ) -> tuple[PerpObservation, ...]:
        oldest = before_utc - timedelta(seconds=max_gap_seconds)
        result: list[PerpObservation] = []
        for venue, market, instrument in keys:
            row = self._connection.execute(
                """
                SELECT payload_json
                FROM observations
                WHERE venue = ?
                  AND market = ?
                  AND instrument = ?
                  AND observed_at_utc < ?
                  AND observed_at_utc >= ?
                ORDER BY observed_at_utc DESC
                LIMIT 1
                """,
                (
                    venue,
                    market,
                    instrument,
                    before_utc.isoformat(),
                    oldest.isoformat(),
                ),
            ).fetchone()
            if row is not None:
                result.append(PerpObservation.model_validate_json(row["payload_json"]))
        return tuple(result)

    def previous_for_current(
        self,
        observations: tuple[PerpObservation, ...],
        *,
        max_gap_seconds: int,
    ) -> tuple[PerpObservation, ...]:
        result: list[PerpObservation] = []
        for observation in observations:
            result.extend(
                self.previous_observations(
                    (observation.key,),
                    before_utc=observation.observed_at_utc,
                    max_gap_seconds=max_gap_seconds,
                )
            )
        return tuple(result)

    def position_states(self) -> dict[str, PositionState]:
        rows = self._connection.execute(
            """
            SELECT target_id, effective_multiplier, pending_multiplier,
                   pending_windows, last_window_id
            FROM position_states
            """
        ).fetchall()
        return {
            str(row["target_id"]): PositionState(
                target_id=str(row["target_id"]),
                effective_multiplier=float(row["effective_multiplier"]),
                pending_multiplier=float(row["pending_multiplier"]),
                pending_windows=int(row["pending_windows"]),
                last_window_id=int(row["last_window_id"]),
            )
            for row in rows
        }

    def persist_snapshot(
        self,
        snapshot: RiskSnapshot,
        *,
        observations: tuple[PerpObservation, ...],
        states: tuple[PositionState, ...],
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO snapshots (
                    snapshot_id, asof_utc, config_hash, payload_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.asof_utc.isoformat(),
                    snapshot.config_hash,
                    snapshot.model_dump_json(),
                ),
            )
            self._connection.executemany(
                """
                INSERT INTO observations (
                    snapshot_id, venue, market, instrument,
                    observed_at_utc, config_hash, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot.snapshot_id,
                        item.venue,
                        item.market,
                        item.instrument,
                        item.observed_at_utc.isoformat(),
                        snapshot.config_hash,
                        item.model_dump_json(),
                    )
                    for item in observations
                ],
            )
            self._connection.executemany(
                """
                INSERT INTO position_states (
                    target_id, effective_multiplier, pending_multiplier,
                    pending_windows, last_window_id, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                    effective_multiplier = excluded.effective_multiplier,
                    pending_multiplier = excluded.pending_multiplier,
                    pending_windows = excluded.pending_windows,
                    last_window_id = excluded.last_window_id,
                    updated_at_utc = excluded.updated_at_utc
                """,
                [
                    (
                        item.target_id,
                        item.effective_multiplier,
                        item.pending_multiplier,
                        item.pending_windows,
                        item.last_window_id,
                        snapshot.asof_utc.isoformat(),
                    )
                    for item in states
                ],
            )

    def latest_snapshot(self) -> RiskSnapshot | None:
        row = self._connection.execute(
            """
            SELECT payload_json
            FROM snapshots
            ORDER BY asof_utc DESC
            LIMIT 1
            """
        ).fetchone()
        return None if row is None else RiskSnapshot.model_validate_json(row["payload_json"])

    def get_snapshot(self, snapshot_id: str) -> RiskSnapshot | None:
        row = self._connection.execute(
            "SELECT payload_json FROM snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        return None if row is None else RiskSnapshot.model_validate_json(row["payload_json"])

    def purge_observation_details(self, *, retention_days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM observations WHERE observed_at_utc < ?",
                (cutoff.isoformat(),),
            )
        return int(cursor.rowcount)

    def record_outcome(self, outcome: OutcomeRecord) -> int:
        snapshot = self.get_snapshot(outcome.snapshot_id)
        if snapshot is None:
            raise ValueError("outcome references an unknown snapshot")
        if outcome.target_id not in {item.target_id for item in snapshot.targets}:
            raise ValueError("outcome references an unknown target")
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO outcomes (
                    snapshot_id, target_id, kind, observed_at_utc,
                    horizon_minutes, return_pct, payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome.snapshot_id,
                    outcome.target_id,
                    outcome.kind,
                    outcome.observed_at_utc.isoformat(),
                    outcome.horizon_minutes,
                    outcome.return_pct,
                    outcome.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("outcome insert did not return an ID")
        return int(cursor.lastrowid)

    def outcomes_with_snapshots(
        self,
    ) -> tuple[tuple[OutcomeRecord, RiskSnapshot], ...]:
        rows = self._connection.execute(
            """
            SELECT o.payload_json AS outcome_json,
                   s.payload_json AS snapshot_json
            FROM outcomes AS o
            JOIN snapshots AS s ON s.snapshot_id = o.snapshot_id
            ORDER BY o.observed_at_utc, o.outcome_id
            """
        ).fetchall()
        return tuple(
            (
                OutcomeRecord.model_validate_json(row["outcome_json"]),
                RiskSnapshot.model_validate_json(row["snapshot_json"]),
            )
            for row in rows
        )

    def save_config_candidate(
        self,
        *,
        candidate_hash: str,
        source_config_hash: str,
        candidate_yaml: str,
        report: dict[str, object],
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO config_candidates (
                    candidate_hash, created_at_utc, status,
                    source_config_hash, candidate_yaml, report_json
                ) VALUES (?, ?, 'candidate', ?, ?, ?)
                """,
                (
                    candidate_hash,
                    datetime.now(UTC).isoformat(),
                    source_config_hash,
                    candidate_yaml,
                    json.dumps(report, sort_keys=True),
                ),
            )

    def approve_config_candidate(self, candidate_hash: str) -> None:
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE config_candidates
                SET status = 'approved'
                WHERE candidate_hash = ? AND status = 'candidate'
                """,
                (candidate_hash,),
            )
            if cursor.rowcount != 1:
                raise ValueError("configuration candidate is unavailable")

    def config_candidate_status(self, candidate_hash: str) -> str | None:
        row = self._connection.execute(
            """
            SELECT status
            FROM config_candidates
            WHERE candidate_hash = ?
            """,
            (candidate_hash,),
        ).fetchone()
        return None if row is None else str(row["status"])

    def get_metadata(self, key: str) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM metadata WHERE key = ?",
            (key,),
        ).fetchone()
        return None if row is None else str(row["value"])

    def set_metadata(self, key: str, value: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO metadata (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def backup_to(self, target: Path) -> Path:
        destination = target.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        backup = sqlite3.connect(destination)
        try:
            self._connection.backup(backup)
            backup.execute("PRAGMA integrity_check")
            backup.commit()
        finally:
            backup.close()
        return destination
