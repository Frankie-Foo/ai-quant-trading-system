"""Platform-neutral command line interface."""

from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .backup import create_backup, restore_backup
from .config import AppConfig, load_config, write_default_config
from .models import OutcomeRecord
from .positioning import recommend_position
from .research import (
    approve_candidate,
    propose_threshold_config,
    review_outcomes,
)
from .schemas import write_schemas
from .secrets import resolve_secret
from .service import RiskService
from .store import RiskStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perp-risk",
        description="Read-only perpetual-market risk positioning.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Strict YAML configuration. Defaults to the packaged config.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="Write a default config.")
    initialize.add_argument("--output", type=Path, required=True)
    initialize.add_argument("--force", action="store_true")

    snapshot = subparsers.add_parser("snapshot", help="Collect one snapshot.")
    snapshot.add_argument("--no-persist", action="store_true")
    snapshot.add_argument("--no-notify", action="store_true")

    watch = subparsers.add_parser("watch", help="Continuously collect snapshots.")
    watch.add_argument(
        "--iterations",
        type=int,
        default=0,
        help="0 means run until interrupted.",
    )
    watch.add_argument("--no-notify", action="store_true")

    subparsers.add_parser("status", help="Read the latest persisted snapshot.")

    recommend = subparsers.add_parser(
        "recommend",
        help="Apply relevant targets to a base position plan.",
    )
    recommend.add_argument(
        "--targets",
        required=True,
        help="Comma-separated risk target IDs.",
    )
    recommend.add_argument("--base-position-pct", type=float)

    outcome = subparsers.add_parser(
        "record-outcome",
        help="Record a benchmark or trade outcome JSON.",
    )
    outcome.add_argument("--file", type=Path, required=True)

    subparsers.add_parser("review", help="Review recorded outcomes.")

    propose = subparsers.add_parser(
        "propose-config",
        help="Generate a non-production threshold challenger.",
    )
    propose.add_argument("--output", type=Path, required=True)

    approve = subparsers.add_parser(
        "approve-config",
        help="Human-gated promotion of a stored candidate.",
    )
    approve.add_argument("--candidate", type=Path, required=True)
    approve.add_argument("--destination", type=Path, required=True)
    approve.add_argument("--confirm", required=True)

    subparsers.add_parser(
        "smoke-live",
        help="Use public live APIs without persisting or notifying.",
    )
    subparsers.add_parser("doctor", help="Validate local configuration and storage.")

    schema = subparsers.add_parser("schema", help="Write integration JSON Schemas.")
    schema.add_argument("--output-dir", type=Path, required=True)

    backup = subparsers.add_parser("backup", help="Create a local SQLite backup.")
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--encrypt", action="store_true")

    restore = subparsers.add_parser("restore", help="Restore a local SQLite backup.")
    restore.add_argument("--input", type=Path, required=True)
    restore.add_argument("--destination", type=Path)
    restore.add_argument("--encrypted", action="store_true")
    restore.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        _print_json({"ok": True, "status": "stopped"})
        return 0
    except Exception as exc:
        _print_json(
            {
                "ok": False,
                "error": type(exc).__name__,
                "message": str(exc),
            }
        )
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "init":
        path = write_default_config(args.output, force=args.force)
        _print_json({"ok": True, "config_path": str(path)})
        return 0
    config = load_config(args.config)
    if args.command == "smoke-live":
        return _smoke_live(config)
    if args.command == "schema":
        paths = write_schemas(args.output_dir)
        _print_json({"ok": True, "schemas": [str(item) for item in paths]})
        return 0
    if args.command == "doctor":
        return _doctor(config)
    if args.command == "restore":
        destination = args.destination or config.database_path
        passphrase = _backup_passphrase() if args.encrypted else None
        restored = restore_backup(
            source=args.input,
            destination=destination,
            passphrase=passphrase,
            force=args.force,
        )
        _print_json({"ok": True, "restored_path": str(restored)})
        return 0
    store = RiskStore(config.database_path)
    try:
        if args.command == "snapshot":
            return _snapshot(
                config,
                store,
                persist=not args.no_persist,
                notify=not args.no_notify,
            )
        if args.command == "watch":
            return _watch(
                config,
                store,
                iterations=args.iterations,
                notify=not args.no_notify,
            )
        if args.command == "status":
            latest = store.latest_snapshot()
            if latest is None:
                _print_json({"ok": False, "status": "no_snapshot"})
                return 1
            print(latest.model_dump_json(indent=2))
            return 0
        if args.command == "recommend":
            latest = store.latest_snapshot()
            if latest is None:
                raise ValueError("no persisted snapshot is available")
            recommendation = recommend_position(
                latest,
                relevant_targets=tuple(args.targets.split(",")),
                base_target_position_pct=args.base_position_pct,
            )
            print(recommendation.model_dump_json(indent=2))
            return 0
        if args.command == "record-outcome":
            outcome = OutcomeRecord.model_validate_json(args.file.read_text(encoding="utf-8"))
            outcome_id = store.record_outcome(outcome)
            _print_json({"ok": True, "outcome_id": outcome_id})
            return 0
        if args.command == "review":
            print(review_outcomes(store).model_dump_json(indent=2))
            return 0
        if args.command == "propose-config":
            path, report = propose_threshold_config(
                store=store,
                config=config,
                output=args.output,
            )
            _print_json(
                {
                    "ok": True,
                    "candidate_path": str(path),
                    "report": report,
                }
            )
            return 0
        if args.command == "approve-config":
            path = approve_candidate(
                store=store,
                candidate_path=args.candidate,
                destination=args.destination,
                confirmation_hash=args.confirm,
            )
            _print_json({"ok": True, "approved_path": str(path)})
            return 0
        if args.command == "backup":
            passphrase = _backup_passphrase() if args.encrypt else None
            path = create_backup(
                store,
                output=args.output,
                passphrase=passphrase,
            )
            _print_json(
                {
                    "ok": True,
                    "backup_path": str(path),
                    "encrypted": args.encrypt,
                }
            )
            return 0
    finally:
        store.close()
    raise ValueError(f"unsupported command: {args.command}")


def _snapshot(
    config: AppConfig,
    store: RiskStore,
    *,
    persist: bool,
    notify: bool,
) -> int:
    service = RiskService(config=config, store=store)
    try:
        result = service.run_snapshot(persist=persist, notify=notify)
    finally:
        service.close()
    payload = result.snapshot.model_dump(mode="json")
    payload["notification_status"] = result.notification_status
    _print_json(payload)
    return 0


def _watch(
    config: AppConfig,
    store: RiskStore,
    *,
    iterations: int,
    notify: bool,
) -> int:
    if iterations < 0:
        raise ValueError("iterations must be non-negative")
    service = RiskService(config=config, store=store)
    completed = 0
    try:
        while iterations == 0 or completed < iterations:
            started = time.monotonic()
            result = service.run_snapshot(persist=True, notify=notify)
            payload = result.snapshot.model_dump(mode="json")
            payload["notification_status"] = result.notification_status
            _print_json(payload)
            completed += 1
            if iterations and completed >= iterations:
                break
            elapsed = time.monotonic() - started
            time.sleep(max(config.collection.interval_seconds - elapsed, 0))
    finally:
        service.close()
    return 0


def _smoke_live(config: AppConfig) -> int:
    with tempfile.TemporaryDirectory(prefix="perp-risk-smoke-") as directory:
        storage = config.storage.model_copy(
            update={
                "database_path": str(Path(directory) / "smoke.sqlite3"),
                "latest_json_path": str(Path(directory) / "latest.json"),
            }
        )
        notification = config.notification.model_copy(update={"enabled": False})
        smoke_config = config.model_copy(
            update={
                "storage": storage,
                "notification": notification,
            }
        )
        store = RiskStore(smoke_config.database_path)
        service = RiskService(config=smoke_config, store=store)
        try:
            result = service.run_snapshot(persist=False, notify=False)
        finally:
            service.close()
            store.close()
    print(result.snapshot.model_dump_json(indent=2))
    return 0


def _doctor(config: AppConfig) -> int:
    database_parent = config.database_path.parent
    database_parent.mkdir(parents=True, exist_ok=True)
    probe = database_parent / ".perp-risk-write-probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    _print_json(
        {
            "ok": True,
            "skill_version": __version__,
            "python_version": platform.python_version(),
            "config_hash": config.config_hash,
            "database_path": str(config.database_path),
            "latest_json_path": str(config.latest_json_path),
            "configured_bindings": len(config.bindings),
            "enabled_targets": [item.target_id for item in config.targets if item.enabled],
            "live_network_checked": False,
            "execution_eligible": False,
        }
    )
    return 0


def _backup_passphrase() -> str:
    value = resolve_secret(
        keyring_service="monitor-perp-risk-positioning",
        keyring_username="backup",
        environment_name="PERP_RISK_BACKUP_PASSPHRASE",
    )
    if not value:
        raise ValueError("backup passphrase is unavailable in keyring or environment")
    return value


def _print_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n"
    )


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
