"""Persistent background workflows for the standalone desktop research client."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

import polars as pl
from pydantic import SecretStr

from data_plane.calendar import build_xnys_schedule
from data_plane.providers.alpaca_proxy import (
    AlpacaProxySipStream,
    AlpacaProxyStreamError,
    fetch_alpaca_proxy_bars,
)
from execution.alpaca_sip_stream import SipEvent
from execution.sip_store import SipEventStore
from operations.adaptive_sip_warmup import build_warmup_events

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class CommandSpec:
    module: str
    arguments: tuple[str, ...]


class CommandRunner(Protocol):
    def run(
        self,
        command: CommandSpec,
        *,
        log_path: Path,
        on_output: Callable[[str], None] | None = None,
    ) -> None: ...


class MonitorAdapter(Protocol):
    def run(
        self,
        *,
        stop_event: threading.Event,
        trade_date: date,
        data_root: Path,
        runs_root: Path,
    ) -> dict[str, object]: ...


class SubprocessCommandRunner:
    def __init__(self, *, environ: Mapping[str, str] | None = None):
        self._environ = dict(os.environ if environ is None else environ)

    def run(
        self,
        command: CommandSpec,
        *,
        log_path: Path,
        on_output: Callable[[str], None] | None = None,
    ) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            [sys.executable, "-m", command.module, *command.arguments],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._environ,
        )
        with log_path.open("w", encoding="utf-8") as log:
            if process.stdout is None:
                raise RuntimeError("child output stream is unavailable")
            for line in process.stdout:
                log.write(line)
                log.flush()
                if on_output is not None:
                    on_output(line.rstrip())
        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError(
                f"{command.module} failed with exit code {return_code}"
            )


class SipStreamLike(Protocol):
    def events(self) -> AsyncGenerator[SipEvent, None]: ...


class SelectionSipMonitor:
    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        stream_factory: Callable[..., SipStreamLike] = AlpacaProxySipStream,
        historical_bars_fetcher: Callable[
            [tuple[str, ...], datetime, datetime], pl.DataFrame
        ] = fetch_alpaca_proxy_bars,
    ):
        self._environ = os.environ if environ is None else environ
        self._stream_factory = stream_factory
        self._historical_bars_fetcher = historical_bars_fetcher

    def run(
        self,
        *,
        stop_event: threading.Event,
        trade_date: date,
        data_root: Path,
        runs_root: Path,
    ) -> dict[str, object]:
        candidates: list[tuple[datetime, Path]] = []
        for path in (data_root / "accepted").glob(
            "kernel.universe.selection_gates-*/data.parquet"
        ):
            frame = pl.read_parquet(path, columns=["session_date"])
            if frame.get_column("session_date").unique().to_list() == [trade_date]:
                candidates.append((path.stat().st_mtime_ns, path))  # type: ignore[arg-type]
        if not candidates:
            raise FileNotFoundError("selection snapshot is required before monitoring")
        _, path = max(candidates)
        frame = pl.read_parquet(path)
        passing = frame.filter(pl.col("pass_gate"))
        if passing.is_empty():
            raise ValueError("selection snapshot has no passing symbols")
        ranked = (
            passing.sort("selection_rank", "symbol")
            if "selection_rank" in passing.columns
            else passing.sort("symbol")
        )
        selection_symbols = tuple(
            str(symbol).strip().upper()
            for symbol in ranked.get_column("symbol").unique(maintain_order=True)
        )
        symbols = tuple(sorted({*selection_symbols, "SPY"}))
        key = str(self._environ.get("ALPACA_PROXY_KEY", "")).strip()
        secret = str(self._environ.get("ALPACA_PROXY_SECRET", "")).strip()
        if not key or not secret:
            raise RuntimeError("Alpaca proxy credentials are missing")
        store = SipEventStore(runs_root / "sip-stream.sqlite3")
        historical_events = self._warm_history(
            store=store,
            trade_date=trade_date,
            symbols=tuple(sorted({*selection_symbols[:12], "SPY"})),
        )

        async def consume() -> int:
            count = 0
            while not stop_event.is_set():
                stream = self._stream_factory(
                    key_id=SecretStr(key),
                    secret_key=SecretStr(secret),
                    symbols=symbols,
                )
                events = stream.events()
                pending: asyncio.Task[SipEvent] | None = None
                try:
                    while not stop_event.is_set():
                        if pending is None:
                            pending = asyncio.create_task(anext(events))
                        done, _ = await asyncio.wait({pending}, timeout=1.0)
                        if not done:
                            continue
                        event = pending.result()
                        pending = None
                        store.append(event)
                        count += 1
                except (StopAsyncIteration, AlpacaProxyStreamError):
                    if not stop_event.is_set():
                        await asyncio.sleep(1.0)
                finally:
                    if pending is not None and not pending.done():
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                    try:
                        await events.aclose()
                    except Exception:
                        pass
            return count

        return {
            "events_stored": asyncio.run(consume()),
            "symbols": list(symbols),
            "historical_events_stored": historical_events,
        }

    def _warm_history(
        self,
        *,
        store: SipEventStore,
        trade_date: date,
        symbols: tuple[str, ...],
    ) -> int:
        schedule = build_xnys_schedule(
            trade_date - timedelta(days=10),
            trade_date,
        )
        if schedule.is_empty():
            raise RuntimeError("SIP warmup schedule is unavailable")
        first_open = schedule.get_column("market_open_utc")[0]
        current = schedule.filter(pl.col("trade_date") == trade_date)
        if current.height != 1 or not isinstance(first_open, datetime):
            raise RuntimeError("SIP warmup schedule is invalid")
        market_close = current.get_column("market_close_utc")[0]
        if not isinstance(market_close, datetime):
            raise RuntimeError("SIP warmup market close is invalid")
        end_utc = min(datetime.now(UTC).replace(second=0, microsecond=0), market_close)
        if end_utc <= first_open:
            return 0
        bars = self._historical_bars_fetcher(symbols, first_open, end_utc)
        events = build_warmup_events(
            bars=bars,
            quotes=pl.DataFrame(),
            trades=pl.DataFrame(),
        )
        store.append_many(events)
        return len(events)


class DesktopWorkflowManager:
    """One interface over resumable sync, selection, and review workflows."""

    ACTIONS = {"sync_data", "run_selection", "run_review", "run_today"}

    def __init__(
        self,
        *,
        data_root: Path,
        runs_root: Path,
        runner: CommandRunner | None = None,
        monitor: MonitorAdapter | None = None,
    ):
        self.data_root = data_root
        self.runs_root = runs_root
        self.runner = runner or SubprocessCommandRunner()
        self.monitor = monitor or SelectionSipMonitor()
        self.state_path = self.runs_root / "desktop-workflows.json"
        self.logs_root = self.runs_root / "desktop-workflow-logs"
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        self._state = self._load_state()
        monitor_state = self._state.get("monitor")
        if isinstance(monitor_state, dict) and monitor_state.get("status") == "running":
            monitor_state["status"] = "interrupted"
        active = self._state.get("active_job")
        if isinstance(active, dict):
            active["status"] = "interrupted"
            active["finished_at_utc"] = datetime.now(UTC).isoformat()
            self._state.setdefault("jobs", []).insert(0, active)
            self._state["active_job"] = None
            self._persist()

    def submit(self, action: str, *, trade_date: date) -> dict[str, Any]:
        if action not in self.ACTIONS:
            raise ValueError(f"unsupported desktop workflow: {action}")
        with self._lock:
            active = self._state.get("active_job")
            if isinstance(active, dict):
                return {
                    "accepted": False,
                    "reason": "workflow_already_running",
                    "active_job": dict(active),
                    "orders_submitted": 0,
                }
            commands = self._commands(action, trade_date)
            job = {
                "job_id": uuid.uuid4().hex,
                "action": action,
                "trade_date": trade_date.isoformat(),
                "status": "running",
                "completed_steps": 0,
                "total_steps": len(commands),
                "current_step": commands[0].module if commands else None,
                "step_progress_current": 0,
                "step_progress_total": 0,
                "step_progress_percent": 0.0,
                "overall_progress_percent": 0.0,
                "progress_detail": None,
                "started_at_utc": datetime.now(UTC).isoformat(),
                "finished_at_utc": None,
                "error": None,
                "orders_submitted": 0,
            }
            self._state["active_job"] = job
            self._persist()
            self._thread = threading.Thread(
                target=self._execute,
                args=(dict(job), commands),
                name=f"desktop-workflow-{action}",
                daemon=True,
            )
            self._thread.start()
            return {
                "accepted": True,
                "job_id": job["job_id"],
                "action": action,
                "orders_submitted": 0,
            }

    def status(self) -> dict[str, Any]:
        with self._lock:
            jobs = self._state.get("jobs", [])
            latest = jobs[0] if isinstance(jobs, list) and jobs else None
            active = self._state.get("active_job")
            return {
                "schema_version": "desktop_workflows.v1",
                "active_job": dict(active) if isinstance(active, dict) else None,
                "latest_job": dict(latest) if isinstance(latest, dict) else None,
                "jobs": [dict(value) for value in jobs[:20] if isinstance(value, dict)],
                "monitor": dict(self._state.get("monitor", {})),
                "data_inventory": self.data_inventory(),
                "orders_submitted": 0,
            }

    def data_inventory(self) -> dict[str, object]:
        accepted = self.data_root / "accepted"
        counts = {
            "grouped_daily": len(list(accepted.glob("massive.grouped_daily-*"))),
            "reference": len(list(accepted.glob("massive.reference_tickers*-*"))),
            "news": len(list(accepted.glob("massive.news.history-*"))),
            "selection": len(list(accepted.glob("kernel.universe.selection_gates-*"))),
            "reviews": len(
                list(accepted.glob("research.intraday_selection_postmortem-*"))
            ),
        }
        return {
            **counts,
            "ready_for_selection": (
                counts["grouped_daily"] >= 21
                and counts["reference"] >= 1
                and counts["news"] >= 1
            ),
        }

    def start_monitor(self, *, trade_date: date) -> dict[str, object]:
        with self._lock:
            current = self._state.get("monitor")
            if isinstance(current, dict) and current.get("status") == "running":
                return {"accepted": False, "reason": "monitor_already_running"}
            self._monitor_stop = threading.Event()
            state: dict[str, object] = {
                "status": "running",
                "trade_date": trade_date.isoformat(),
                "started_at_utc": datetime.now(UTC).isoformat(),
                "finished_at_utc": None,
                "events_stored": 0,
                "symbols": [],
                "error": None,
            }
            self._state["monitor"] = state
            self._persist()
            self._monitor_thread = threading.Thread(
                target=self._run_monitor,
                args=(trade_date,),
                name="desktop-intraday-monitor",
                daemon=True,
            )
            self._monitor_thread.start()
            return {"accepted": True, "orders_submitted": 0}

    def stop_monitor(self, *, timeout_seconds: float = 5.0) -> dict[str, object]:
        self._monitor_stop.set()
        thread = self._monitor_thread
        if thread is not None:
            thread.join(timeout=timeout_seconds)
        with self._lock:
            state = self._state.get("monitor")
            if isinstance(state, dict) and state.get("status") == "running":
                state["status"] = "stopped"
                state["finished_at_utc"] = datetime.now(UTC).isoformat()
                self._persist()
            return {"stopped": thread is None or not thread.is_alive(), "orders_submitted": 0}

    def _run_monitor(self, trade_date: date) -> None:
        try:
            result = self.monitor.run(
                stop_event=self._monitor_stop,
                trade_date=trade_date,
                data_root=self.data_root,
                runs_root=self.runs_root,
            )
            status = "stopped" if self._monitor_stop.is_set() else "complete"
            error = None
        except Exception as exc:
            result = {}
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
        with self._lock:
            state = self._state.get("monitor")
            if isinstance(state, dict):
                state.update(result)
                state["status"] = status
                state["error"] = error
                state["finished_at_utc"] = datetime.now(UTC).isoformat()
                self._persist()

    def wait_for_idle(self, *, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            thread = self._thread
            if thread is None or not thread.is_alive():
                return
            thread.join(timeout=0.05)
        raise TimeoutError("desktop workflow did not finish before timeout")

    def _execute(self, job: dict[str, Any], commands: tuple[CommandSpec, ...]) -> None:
        try:
            for index, command in enumerate(commands, start=1):
                with self._lock:
                    active = self._state.get("active_job")
                    if not isinstance(active, dict):
                        raise RuntimeError("desktop workflow state was lost")
                    active["current_step"] = command.module
                    self._persist()
                def handle_output(line: str, current_step: int = index) -> None:
                    self._record_output(
                        str(job["job_id"]),
                        current_step,
                        line,
                    )

                self.runner.run(
                    command,
                    log_path=self.logs_root
                    / str(job["job_id"])
                    / f"{index:02d}-{command.module.replace('.', '_')}.log",
                    on_output=handle_output,
                )
                with self._lock:
                    active = self._state.get("active_job")
                    if isinstance(active, dict):
                        active["completed_steps"] = index
                        active["step_progress_current"] = 0
                        active["step_progress_total"] = 0
                        active["step_progress_percent"] = 0.0
                        active["overall_progress_percent"] = round(
                            index / len(commands) * 100,
                            2,
                        )
                        self._persist()
            job["status"] = "complete"
            if job.get("action") == "run_today":
                self.start_monitor(
                    trade_date=date.fromisoformat(str(job["trade_date"]))
                )
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            job["completed_steps"] = int(
                self._state.get("active_job", {}).get("completed_steps", 0)
                if isinstance(self._state.get("active_job"), dict)
                else job.get("completed_steps", 0)
            )
            job["current_step"] = None
            job["finished_at_utc"] = datetime.now(UTC).isoformat()
            with self._lock:
                jobs = self._state.setdefault("jobs", [])
                if not isinstance(jobs, list):
                    jobs = []
                    self._state["jobs"] = jobs
                jobs.insert(0, job)
                del jobs[20:]
                self._state["active_job"] = None
                self._persist()

    def _record_output(self, job_id: str, step: int, line: str) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        raw_progress = payload.get("progress")
        if not isinstance(raw_progress, str) or "/" not in raw_progress:
            return
        try:
            current_text, total_text = raw_progress.split("/", 1)
            current = int(current_text)
            total = int(total_text)
        except ValueError:
            return
        if current < 0 or total <= 0 or current > total:
            return
        with self._lock:
            active = self._state.get("active_job")
            if not isinstance(active, dict) or active.get("job_id") != job_id:
                return
            total_steps = int(active.get("total_steps", 1))
            active["step_progress_current"] = current
            active["step_progress_total"] = total
            active["step_progress_percent"] = round(current / total * 100, 2)
            active["overall_progress_percent"] = round(
                ((step - 1) + current / total) / total_steps * 100,
                2,
            )
            active["progress_detail"] = (
                payload.get("trade_date")
                or payload.get("stage")
                or payload.get("symbol")
            )
            self._persist()

    def _commands(self, action: str, trade_date: date) -> tuple[CommandSpec, ...]:
        previous = self._previous_session(trade_date)
        data_root = str(self.data_root)
        if action == "sync_data":
            minimum_start = trade_date - timedelta(days=550)
            latest_daily = self._latest_grouped_daily_date()
            start = (
                max(minimum_start, latest_daily + timedelta(days=1))
                if latest_daily is not None
                else minimum_start
            )
            if start > previous:
                start = previous
            news_start = trade_date - timedelta(days=60)
            return (
                CommandSpec(
                    "data_plane.cli",
                    (
                        "--data-root", data_root,
                        "massive-grouped-daily",
                        "--start", start.isoformat(),
                        "--end", previous.isoformat(),
                    ),
                ),
                CommandSpec(
                    "scripts.backfill_massive_reference_weekly",
                    (
                        "--end", previous.isoformat(),
                        "--sessions", "252",
                        "--data-root", data_root,
                    ),
                ),
                CommandSpec(
                    "scripts.backfill_massive_news",
                    (
                        "--start", news_start.isoformat(),
                        "--end", (trade_date + timedelta(days=1)).isoformat(),
                        "--data-root", data_root,
                    ),
                ),
            )
        if action == "run_today":
            minimum_start = trade_date - timedelta(days=550)
            latest_daily = self._latest_grouped_daily_date()
            start = (
                max(minimum_start, latest_daily + timedelta(days=1))
                if latest_daily is not None
                else minimum_start
            )
            if start > previous:
                start = previous
            common = ("--trade-date", trade_date.isoformat(), "--data-root", data_root)
            return (
                CommandSpec(
                    "data_plane.cli",
                    (
                        "--data-root", data_root,
                        "massive-grouped-daily",
                        "--start", start.isoformat(),
                        "--end", previous.isoformat(),
                    ),
                ),
                CommandSpec(
                    "data_plane.cli",
                    (
                        "--data-root", data_root,
                        "massive-reference",
                        "--date", previous.isoformat(),
                    ),
                ),
                CommandSpec(
                    "scripts.backfill_massive_news",
                    (
                        "--start", (trade_date - timedelta(days=60)).isoformat(),
                        "--end", (trade_date + timedelta(days=1)).isoformat(),
                        "--data-root", data_root,
                    ),
                ),
                CommandSpec("scripts.build_daily_universe", common),
                *self._pending_today_commands(common, trade_date),
            )
        if action == "run_selection":
            common = ("--trade-date", trade_date.isoformat(), "--data-root", data_root)
            return (
                CommandSpec(
                    "data_plane.cli",
                    (
                        "--data-root", data_root,
                        "massive-grouped-daily",
                        "--start", previous.isoformat(),
                        "--end", previous.isoformat(),
                    ),
                ),
                CommandSpec(
                    "data_plane.cli",
                    (
                        "--data-root", data_root,
                        "massive-reference",
                        "--date", previous.isoformat(),
                    ),
                ),
                CommandSpec("scripts.build_daily_universe", common),
                CommandSpec(
                    "scripts.build_catalyst_snapshot",
                    (*common, "--massive-pace-seconds", "0.25"),
                ),
                CommandSpec("scripts.build_premarket_rvol", common),
                CommandSpec(
                    "scripts.build_selection_gates",
                    (*common, "--massive-pace-seconds", "0.25"),
                ),
            )
        return (
            CommandSpec(
                "scripts.run_postclose_missed_movers_review",
                (
                    "--trade-date", trade_date.isoformat(),
                    "--data-root", data_root,
                    "--top", "8",
                    "--attempts", "3",
                ),
            ),
        )

    def _latest_grouped_daily_date(self) -> date | None:
        latest: date | None = None
        for path in (self.data_root / "accepted").glob(
            "massive.grouped_daily-*/data.parquet"
        ):
            try:
                values = pl.read_parquet(
                    path,
                    columns=["trade_date"],
                ).get_column("trade_date")
            except (OSError, ValueError):
                continue
            for value in values.to_list():
                if isinstance(value, date) and (latest is None or value > latest):
                    latest = value
        return latest

    def _pending_today_commands(
        self,
        common: tuple[str, ...],
        trade_date: date,
    ) -> tuple[CommandSpec, ...]:
        commands: list[CommandSpec] = []
        if not self._has_snapshot(
            "kernel.catalysts.overnight_candidates",
            "session_date",
            trade_date,
        ):
            commands.append(
                CommandSpec(
                    "scripts.build_catalyst_snapshot",
                    (*common, "--massive-pace-seconds", "0.25"),
                )
            )
        if not self._has_snapshot(
            "kernel.premarket.rvol_candidates",
            "session_date",
            trade_date,
        ):
            commands.append(CommandSpec("scripts.build_premarket_rvol", common))
        if not self._has_snapshot(
            "kernel.universe.selection_gates",
            "session_date",
            trade_date,
        ):
            commands.append(
                CommandSpec(
                    "scripts.build_selection_gates",
                    (*common, "--massive-pace-seconds", "0.25"),
                )
            )
        return tuple(commands)

    def _has_snapshot(self, prefix: str, column: str, target: date) -> bool:
        for path in (self.data_root / "accepted").glob(f"{prefix}-*/data.parquet"):
            try:
                values = pl.read_parquet(path, columns=[column])[column].unique().to_list()
            except (OSError, ValueError):
                continue
            if values == [target]:
                return True
        return False

    @staticmethod
    def _previous_session(trade_date: date) -> date:
        schedule = build_xnys_schedule(trade_date - timedelta(days=10), trade_date)
        values = schedule.filter(schedule["trade_date"] < trade_date)["trade_date"].tail(1)
        if len(values) != 1 or not isinstance(values[0], date):
            raise ValueError("previous XNYS session is unavailable")
        return values[0]

    def _load_state(self) -> dict[str, Any]:
        empty: dict[str, Any] = {
            "schema_version": "desktop_workflows.v1",
            "active_job": None,
            "jobs": [],
            "monitor": {"status": "stopped"},
        }
        if not self.state_path.is_file():
            return empty
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return empty
        return cast(dict[str, Any], value) if isinstance(value, dict) else empty

    def _persist(self) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.state_path)
