"""Cross-platform single-process lock and structured operational logging."""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import IO, Any


class LockUnavailableError(RuntimeError):
    """Raised when another scheduler process owns the same lock."""


class ProcessLock:
    """Advisory lock that keeps a stable file inode to avoid delete/recreate races."""

    _process_guard = threading.Lock()
    _held_paths: set[Path] = set()

    def __init__(self, path: Path):
        self.path = path.resolve()
        self._handle: IO[str] | None = None

    def __enter__(self) -> ProcessLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._process_guard:
            if self.path in self._held_paths:
                raise LockUnavailableError(f"lock already held: {self.path.name}")
            self._held_paths.add(self.path)
        handle: IO[str] | None = None
        try:
            handle = self.path.open("a+", encoding="utf-8")
            if self.path.stat().st_size == 0:
                handle.write(" ")
                handle.flush()
            handle.seek(0)
            self._lock_handle(handle)
            handle.seek(0)
            handle.truncate()
            handle.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "started_at_utc": datetime.now(UTC).isoformat(),
                    },
                    separators=(",", ":"),
                )
            )
            handle.flush()
            os.fsync(handle.fileno())
            self._handle = handle
            return self
        except Exception:
            if handle is not None:
                handle.close()
            with self._process_guard:
                self._held_paths.discard(self.path)
            raise

    @staticmethod
    def _lock_handle(handle: IO[str]) -> None:
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise LockUnavailableError("scheduler lock is held") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise LockUnavailableError("scheduler lock is held") from exc

    @staticmethod
    def _unlock_handle(handle: IO[str]) -> None:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        handle = self._handle
        try:
            if handle is not None:
                self._unlock_handle(handle)
                handle.close()
        finally:
            self._handle = None
            with self._process_guard:
                self._held_paths.discard(self.path)


class JsonEventLogger:
    def __init__(self, *, stream: IO[str] = sys.stdout, service: str):
        if not service.strip():
            raise ValueError("service is required")
        self.stream = stream
        self.service = service

    def emit(self, event: str, *, level: str = "info", **fields: Any) -> None:
        if not event.strip():
            raise ValueError("event is required")
        payload = {
            "ts_utc": datetime.now(UTC).isoformat(),
            "level": level,
            "service": self.service,
            "event": event,
            **fields,
        }
        self.stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        self.stream.flush()
