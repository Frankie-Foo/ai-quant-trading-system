"""Run the non-executable factor/order-flow/unified shadow pipeline in dependency order."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from data_plane.snapshot_queries import load_latest_session_snapshot

ROOT = Path(__file__).resolve().parents[1]
STAGE_SOURCES = {
    "cross_asset_sentiment": "kernel.cross_asset.sentiment_shadow",
    "factor_rvol": "kernel.premarket.factor_rvol_candidates",
    "factor_candidates": "kernel.selection.factor_candidates_shadow",
    "order_flow": "kernel.features.order_flow_shadow",
    "unified_arbitration": "kernel.selection.unified_shadow",
}


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _parse_asof(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("asof must be an ISO-8601 timestamp") from exc
    if result.tzinfo is None:
        raise argparse.ArgumentTypeError("asof must include a timezone")
    return result.astimezone(UTC)


def shadow_pipeline_commands(
    *,
    trade_date: date,
    data_root: Path,
    asof_utc: datetime | None,
) -> tuple[tuple[str, list[str]], ...]:
    """Return the complete shadow-only DAG as inspectable child commands."""

    common = [
        "--trade-date",
        trade_date.isoformat(),
        "--data-root",
        str(data_root),
    ]
    order_flow = ["-m", "scripts.build_order_flow_snapshot", *common]
    cross_asset = [
        "-m",
        "scripts.build_cross_asset_sentiment_snapshot",
        *common,
    ]
    if asof_utc is not None:
        cross_asset.extend(
            ["--asof-utc", asof_utc.astimezone(UTC).isoformat()]
        )
        order_flow.extend(["--asof-utc", asof_utc.astimezone(UTC).isoformat()])
    return (
        ("cross_asset_sentiment", cross_asset),
        (
            "factor_rvol",
            [
                "-m",
                "scripts.build_premarket_rvol",
                *common,
                "--pool",
                "factor",
            ],
        ),
        (
            "factor_candidates",
            ["-m", "scripts.build_factor_candidates", *common],
        ),
        ("order_flow", order_flow),
        (
            "unified_arbitration",
            ["-m", "scripts.build_unified_shadow_selection", *common],
        ),
    )


def _dataset_ids(stdout: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(re.findall(r'"dataset_id"\s*:\s*"([^"]+)"', stdout))
    )


def stage_snapshot_reusable(
    stage: str,
    frame: pl.DataFrame,
    *,
    requested_asof_utc: datetime | None,
) -> bool:
    """Prevent volatile live evidence from being reused across decision times."""

    if stage != "cross_asset_sentiment":
        return True
    if requested_asof_utc is None:
        return False
    if frame.is_empty() or "data_cutoff_utc" not in frame.columns:
        return False
    cutoffs = frame.get_column("data_cutoff_utc").drop_nulls().unique().to_list()
    return len(cutoffs) == 1 and cutoffs[0] == requested_asof_utc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_parse_date, required=True)
    parser.add_argument("--asof-utc", type=_parse_asof)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    args = parser.parse_args()

    stages: list[dict[str, object]] = []
    for stage, command in shadow_pipeline_commands(
        trade_date=args.trade_date,
        data_root=args.data_root,
        asof_utc=args.asof_utc,
    ):
        try:
            frame, existing = load_latest_session_snapshot(
                args.data_root,
                source=STAGE_SOURCES[stage],
                session_date=args.trade_date,
            )
        except FileNotFoundError:
            existing = None
        if (
            existing is not None
            and stage_snapshot_reusable(
                stage,
                frame,
                requested_asof_utc=args.asof_utc,
            )
        ):
            stages.append(
                {
                    "stage": stage,
                    "return_code": 0,
                    "dataset_ids": (existing.dataset_id,),
                    "status": "reused",
                }
            )
            continue
        completed = subprocess.run(
            [sys.executable, *command],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )
        evidence = {
            "stage": stage,
            "return_code": completed.returncode,
            "dataset_ids": _dataset_ids(completed.stdout),
            "stdout_lines": len(completed.stdout.splitlines()),
            "stderr_lines": len(completed.stderr.splitlines()),
        }
        stages.append(evidence)
        if completed.returncode != 0:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "status": "shadow_failed",
                        "failed_stage": stage,
                        "stages": stages,
                        "orders_submitted": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            raise SystemExit(completed.returncode)

    print(
        json.dumps(
            {
                "ok": True,
                "status": "shadow_complete",
                "trade_date": args.trade_date.isoformat(),
                "stages": stages,
                "production_eligible": False,
                "execution_eligible": False,
                "orders_submitted": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
