"""Compare frozen Champion and non-executable Challenger selection lists post-close."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.storage import persist_snapshot, sha256_file

ROOT = Path(__file__).resolve().parents[1]
POSTMORTEM_SOURCE = "research.intraday_selection_postmortem"
SHADOW_SOURCE = "research.strategy_shadow_outcome"


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return value


def _symbols(rows: object) -> tuple[str, ...]:
    if not isinstance(rows, list):
        raise ValueError("strategy candidate rows must be a list")
    symbols = tuple(
        str(row.get("symbol", "")).strip().upper()
        for row in rows
        if isinstance(row, dict)
    )
    if len(symbols) != len(rows) or any(not value for value in symbols):
        raise ValueError("strategy candidate identities are incomplete")
    if len(symbols) != len(set(symbols)):
        raise ValueError("strategy candidate identities are duplicated")
    return symbols


def evaluate_shadow(
    first_wave_path: Path,
    postmortem_path: Path,
    postmortem_snapshot: DatasetSnapshot,
    *,
    trade_date: date,
) -> pl.DataFrame:
    first_wave = _read_object(first_wave_path)
    if first_wave.get("trade_date") != trade_date.isoformat():
        raise ValueError("first-wave trade date mismatch")
    context = first_wave.get("strategy_context")
    if not isinstance(context, dict):
        raise ValueError("first-wave strategy context is missing")
    challenger = context.get("challenger")
    if not isinstance(challenger, dict):
        raise ValueError("first-wave challenger is missing")
    if challenger.get("execution_eligible") is not False:
        raise ValueError("shadow challenger crossed the execution boundary")
    policy_hash = str(challenger.get("policy_hash", ""))
    if re.fullmatch(r"[0-9a-f]{64}", policy_hash) is None:
        raise ValueError("shadow challenger policy hash is missing or invalid")
    champion = _symbols(first_wave.get("candidates"))
    challenger_symbols = tuple(
        str(value).strip().upper() for value in challenger.get("symbols", [])
    )
    if len(challenger_symbols) != len(set(challenger_symbols)):
        raise ValueError("challenger symbols are duplicated")
    if not set(challenger_symbols).issubset(champion):
        raise ValueError("challenger must be a subset of the frozen Champion pool")
    postmortem_snapshot.assert_usable()
    if sha256_file(postmortem_path) != postmortem_snapshot.content_sha256:
        raise ValueError("postmortem snapshot hash mismatch")
    postmortem = pl.read_parquet(postmortem_path)
    required = {"session_date", "symbol", "close_return"}
    if missing := required - set(postmortem.columns):
        raise ValueError(f"postmortem fields missing: {sorted(missing)}")
    dates = postmortem["session_date"].unique().to_list()
    if dates != [trade_date]:
        raise ValueError("postmortem trade date mismatch")
    opportunity_symbols = set(postmortem["symbol"].to_list())
    evidence_complete = postmortem.height > 0 and not postmortem.select(
        pl.any_horizontal(
            pl.col("symbol").is_null(), pl.col("close_return").is_null()
        ).any()
    ).item()
    return pl.DataFrame(
        {
            "session_date": [trade_date],
            "active_version": [str(context.get("active_version", ""))],
            "active_policy_hash": [str(context.get("active_policy_hash", ""))],
            "challenger_version": [str(challenger.get("version", ""))],
            "challenger_policy_hash": [policy_hash],
            "first_wave_sha256": [hashlib.sha256(first_wave_path.read_bytes()).hexdigest()],
            "champion_candidate_count": [len(champion)],
            "challenger_candidate_count": [len(challenger_symbols)],
            "champion_capture_count": [len(set(champion) & opportunity_symbols)],
            "challenger_capture_count": [len(set(challenger_symbols) & opportunity_symbols)],
            "evidence_complete": [evidence_complete],
            "orders_submitted": [0],
        }
    )


def _latest_postmortem(
    data_root: Path, trade_date: date
) -> tuple[Path, DatasetSnapshot]:
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(f"{POSTMORTEM_SOURCE}-*/data.parquet"):
        frame = pl.read_parquet(path, columns=["session_date"])
        if frame["session_date"].unique().to_list() != [trade_date]:
            continue
        snapshot = DatasetSnapshot.model_validate_json(
            (path.parent / "manifest.json").read_text(encoding="utf-8")
        ).assert_usable()
        if (
            snapshot.source != POSTMORTEM_SOURCE
            or snapshot.schema_version != "intraday_selection_postmortem.v1"
            or snapshot.dataset_id != path.parent.name
            or sha256_file(path) != snapshot.content_sha256
        ):
            raise ValueError("accepted postmortem identity or content hash mismatch")
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        raise FileNotFoundError("accepted selection postmortem is unavailable")
    _, path, snapshot = max(matches, key=lambda item: item[0])
    return path, snapshot


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True, type=date.fromisoformat)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--state-root", type=Path, default=ROOT / "runs/autonomous")
    args = parser.parse_args(argv)
    first_wave = args.state_root / args.trade_date.isoformat() / "first_wave_pool.json"
    if not first_wave.is_file():
        raise FileNotFoundError("frozen first-wave artifact is unavailable")
    payload = _read_object(first_wave)
    context = payload.get("strategy_context")
    if not isinstance(context, dict) or context.get("challenger") is None:
        print(json.dumps({"status": "no_challenger", "orders_submitted": 0}))
        return 0
    postmortem_path, postmortem_snapshot = _latest_postmortem(
        args.data_root, args.trade_date
    )
    frame = evaluate_shadow(
        first_wave,
        postmortem_path,
        postmortem_snapshot,
        trade_date=args.trade_date,
    )
    snapshot, path = persist_snapshot(
        frame,
        root=args.data_root,
        source=SHADOW_SOURCE,
        schema_version="strategy_shadow_outcome.v1",
        checks=(
            DataQualityCheck(
                name="shadow_cannot_trade",
                severity=QualitySeverity.CRITICAL,
                passed=frame["orders_submitted"].to_list() == [0],
                observed=str(frame["orders_submitted"].to_list()),
                expected="[0]",
                provenance="scripts.evaluate_strategy_shadow.v1",
            ),
        ),
        parent_snapshot_ids=(postmortem_snapshot.dataset_id,),
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
                "orders_submitted": 0,
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
