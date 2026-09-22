"""Offline, append-only SQLite event revisions. No fetching or broker integration."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from data_plane.contracts import EventObservation, EventRevision


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("ledger time must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


class EventLedger:
    """One source/id is an event; equal substantive payloads are idempotent on restart.

    Each instance serializes access; SQLite serializes writers across instances and
    processes. A backwards clock rejects new writes instead of inventing a time.
    """

    def __init__(self, path: str | Path, clock: Callable[[], datetime] = _now) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(path), timeout=30, isolation_level=None, check_same_thread=False
        )
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA recursive_triggers=ON")
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS event_revisions (
                    event_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    payload_hash TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    available_at TEXT NOT NULL CHECK (available_at >= recorded_at),
                    revision_json TEXT NOT NULL,
                    PRIMARY KEY (event_id, revision),
                    UNIQUE (event_id, payload_hash)
                )"""
            )
            self._connection.execute(
                """CREATE INDEX IF NOT EXISTS event_revisions_recorded
                   ON event_revisions(recorded_at)"""
            )
            self._connection.execute(
                """CREATE TRIGGER IF NOT EXISTS event_revisions_no_update
                   BEFORE UPDATE ON event_revisions BEGIN
                   SELECT RAISE(ABORT, 'event revisions are append-only'); END"""
            )
            self._connection.execute(
                """CREATE TRIGGER IF NOT EXISTS event_revisions_no_delete
                   BEFORE DELETE ON event_revisions BEGIN
                   SELECT RAISE(ABORT, 'event revisions are append-only'); END"""
            )
            self._connection.execute(
                """CREATE TRIGGER IF NOT EXISTS event_revisions_ordered_insert
                   BEFORE INSERT ON event_revisions BEGIN
                   SELECT CASE WHEN NEW.revision != COALESCE(
                       (SELECT MAX(revision) FROM event_revisions
                        WHERE event_id = NEW.event_id), 0) + 1
                       THEN RAISE(ABORT, 'event revision must be sequential') END;
                   SELECT CASE WHEN NEW.recorded_at <
                       (SELECT MAX(recorded_at) FROM event_revisions)
                       THEN RAISE(ABORT, 'ledger clock moved backwards') END;
                   SELECT CASE WHEN NEW.available_at <
                       (SELECT MAX(available_at) FROM event_revisions
                        WHERE event_id = NEW.event_id)
                       THEN RAISE(ABORT, 'event availability moved backwards') END;
                   SELECT CASE WHEN EXISTS (
                       SELECT 1 FROM event_revisions WHERE event_id = NEW.event_id
                       AND payload_hash = NEW.payload_hash)
                       THEN RAISE(ABORT, 'event payload already recorded') END;
                   END"""
            )
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            self._connection.close()
            raise

    def append(self, observation: EventObservation) -> EventRevision:
        return self.append_many((observation,))[0]

    def append_many(self, observations: Iterable[EventObservation]) -> tuple[EventRevision, ...]:
        """Validate the entire batch before writing; any failure rolls it all back.

        First-seen is excluded from the fingerprint: polling cannot change the
        original evidence or upgrade a version with missing first-seen. Origin is
        fixed on first insertion. Re-importing an old payload returns that original
        revision, never makes it the latest revision again.
        """
        batch = tuple(EventObservation.model_validate(item) for item in observations)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                result = tuple(self._append(item) for item in batch)
                self._connection.execute("COMMIT")
                return result
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def _append(self, observation: EventObservation) -> EventRevision:
        event_id = _hash((observation.source, observation.source_event_id))
        original = self._connection.execute(
            "SELECT revision_json FROM event_revisions WHERE event_id = ? AND revision = 1",
            (event_id,),
        ).fetchone()
        if original is not None:
            original_origin = EventRevision.model_validate_json(original[0]).observation.origin
            if observation.origin != original_origin:
                raise ValueError("event origin cannot change within a ledger")
        payload_hash = _hash(observation.model_dump(mode="json", exclude={"first_seen_at"}))
        existing = self._connection.execute(
            "SELECT revision_json FROM event_revisions WHERE event_id = ? AND payload_hash = ?",
            (event_id, payload_hash),
        ).fetchone()
        if existing is not None:
            return EventRevision.model_validate_json(existing[0])

        recorded_at = _utc(self._clock())
        last_recorded = self._connection.execute(
            "SELECT MAX(recorded_at) FROM event_revisions"
        ).fetchone()[0]
        if last_recorded is not None and _timestamp(recorded_at) < last_recorded:
            raise ValueError("ledger clock moved backwards")
        previous = self._connection.execute(
            """SELECT revision, available_at FROM event_revisions
               WHERE event_id = ? ORDER BY revision DESC LIMIT 1""",
            (event_id,),
        ).fetchone()
        times = (
            recorded_at,
            observation.published_at,
            observation.updated_at,
            observation.first_seen_at,
            datetime.fromisoformat(previous[1]) if previous else None,
        )
        # Whitespace only: retain case, punctuation and headline/body boundaries.
        body = tuple(
            " ".join((text or "").split())
            for text in (observation.headline, observation.summary)
        )
        body_hash = _hash(body)
        # ponytail: exact symbol-set clustering may miss partial multi-symbol copies;
        # retain separate clusters until evidence supports a finer per-symbol model.
        cluster_key: object = (
            observation.symbols, body_hash, observation.published_at.date().isoformat()
        )
        if not any(body):
            cluster_key = (event_id, payload_hash)  # Empty text is not copy evidence.
        revision = EventRevision(
            event_id=event_id,
            revision=previous[0] + 1 if previous else 1,
            event_cluster_id=_hash(cluster_key),
            body_hash=body_hash,
            observation=observation,
            recorded_at=recorded_at,
            available_at=max(value for value in times if value is not None),
            forward_eligible=(
                observation.origin == "forward" and observation.first_seen_at is not None
            ),
        )
        self._connection.execute(
            """INSERT INTO event_revisions
               (event_id, revision, payload_hash, recorded_at, available_at, revision_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                event_id, revision.revision, payload_hash, _timestamp(recorded_at),
                _timestamp(revision.available_at), revision.model_dump_json(),
            ),
        )
        return revision

    def revisions(self) -> tuple[EventRevision, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT revision_json FROM event_revisions ORDER BY event_id, revision"
            ).fetchall()
        return tuple(EventRevision.model_validate_json(row[0]) for row in rows)

    def as_of(self, at: datetime, forward_only: bool = True) -> tuple[EventRevision, ...]:
        """Latest available version per event, then apply the forward restriction."""
        timestamp = _timestamp(at)
        with self._lock:
            rows = self._connection.execute(
                """SELECT r.revision_json FROM event_revisions AS r
                   JOIN (
                       SELECT event_id, MAX(revision) AS revision FROM event_revisions
                       WHERE available_at <= ? AND recorded_at <= ? GROUP BY event_id
                   ) AS latest ON r.event_id = latest.event_id AND r.revision = latest.revision
                   ORDER BY r.event_id, r.revision""",
                (timestamp, timestamp),
            ).fetchall()
        revisions = tuple(EventRevision.model_validate_json(row[0]) for row in rows)
        return tuple(item for item in revisions if not forward_only or item.forward_eligible)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
