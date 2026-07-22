from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
from dotenv import load_dotenv

from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.storage import persist_snapshot
from research.catalyst_scoring import CatalystScore, score_intraday_continuation
from research.providers.deepseek import DEEPSEEK_MODEL, DeepSeekClient

ROOT = Path(__file__).resolve().parents[1]
BEIJING = ZoneInfo("Asia/Shanghai")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("limit must be positive")
    return parsed


def _manifest(path: Path) -> DatasetSnapshot:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return DatasetSnapshot.model_validate(payload)


def _load_candidates(
    data_root: Path, trade_date: date
) -> tuple[pl.DataFrame, DatasetSnapshot]:
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    pattern = "kernel.catalysts.overnight_candidates-*/data.parquet"
    for path in (data_root / "accepted").glob(pattern):
        frame = pl.read_parquet(path, columns=["session_date"])
        if frame.get_column("session_date").unique().to_list() != [trade_date]:
            continue
        snapshot = _manifest(path.parent / "manifest.json")
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        raise FileNotFoundError(f"no locked catalyst candidates for {trade_date}")
    _, path, snapshot = max(matches)
    return pl.read_parquet(path), snapshot


def _load_prepared_parent(
    data_root: Path, candidate_snapshot: DatasetSnapshot
) -> tuple[pl.DataFrame, DatasetSnapshot]:
    ids = [
        value
        for value in candidate_snapshot.parent_snapshot_ids
        if value.startswith("kernel.catalysts.prepared-")
    ]
    if len(ids) != 1:
        raise ValueError("candidate snapshot must have exactly one prepared parent")
    path = data_root / "accepted" / ids[0] / "data.parquet"
    if not path.exists():
        raise FileNotFoundError(f"prepared parent is unavailable: {ids[0]}")
    return pl.read_parquet(path), _manifest(path.parent / "manifest.json")


def _event_text(row: dict[str, object]) -> str:
    parts = [
        f"published_utc={row.get('published_utc')}",
        f"event_type={row.get('event_type') or 'unknown'}",
        f"event_subtype={row.get('event_subtype') or 'unknown'}",
        f"category={row.get('catalyst_category') or 'unknown'}",
    ]
    form_items = row.get("form_items")
    if isinstance(form_items, list) and form_items:
        parts.append(f"form_items={','.join(str(item) for item in form_items)}")
    if row.get("headline"):
        parts.append(f"headline={row['headline']}")
    if row.get("summary"):
        parts.append(f"summary={row['summary']}")
    return " | ".join(parts)


def build_symbol_evidence(
    candidates: pl.DataFrame,
    prepared: pl.DataFrame,
    *,
    asof_utc: datetime,
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Resolve only evidence IDs frozen into the candidate lock."""
    if asof_utc.tzinfo is None or asof_utc.utcoffset() is None:
        raise ValueError("asof_utc must be timezone-aware")
    required_candidates = {"symbol", "evidence_event_ids"}
    required_prepared = {
        "source",
        "source_event_id",
        "published_utc",
        "event_type",
        "event_subtype",
        "headline",
        "summary",
        "form_items",
        "catalyst_category",
    }
    if missing := required_candidates - set(candidates.columns):
        raise ValueError(f"candidates missing columns: {sorted(missing)}")
    if missing := required_prepared - set(prepared.columns):
        raise ValueError(f"prepared evidence missing columns: {sorted(missing)}")

    visible = prepared.filter(pl.col("published_utc") <= asof_utc).with_columns(
        pl.concat_str("source", "source_event_id", separator=":").alias("evidence_id")
    )
    if visible.height != visible.get_column("evidence_id").n_unique():
        raise ValueError("prepared evidence contains duplicate provider event IDs")
    by_id = {
        str(row["evidence_id"]): row for row in visible.iter_rows(named=True)
    }
    output: dict[str, tuple[tuple[str, str], ...]] = {}
    for candidate in candidates.sort("symbol").iter_rows(named=True):
        symbol = str(candidate["symbol"])
        raw_ids = candidate["evidence_event_ids"]
        ids = tuple(str(value) for value in raw_ids) if isinstance(raw_ids, list) else ()
        missing_ids = sorted(set(ids) - set(by_id))
        if missing_ids:
            raise ValueError(
                f"missing locked evidence for {symbol}: {len(missing_ids)} event(s)"
            )
        output[symbol] = tuple((event_id, _event_text(by_id[event_id])) for event_id in ids)
    return output


def _check(
    name: str,
    passed: bool,
    observed: object,
    expected: str,
) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=QualitySeverity.CRITICAL,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance="research.catalysts.deepseek_v4_pro_shadow.v1",
    )


def _score_row(score: CatalystScore, trade_date: date) -> dict[str, object]:
    return {
        "symbol": score.symbol,
        "session_date": trade_date,
        "raw_probability": score.probability,
        "score_asof_utc": score.asof_utc,
        "model_id": score.model_id,
        "temperature": score.temperature,
        "prompt_sha256": score.prompt_sha256,
        "evidence_event_ids": list(score.evidence_ids),
        "provider_request_id": score.provider_request_id,
        "response_model": score.response_model,
        "system_fingerprint": score.system_fingerprint,
        "prompt_tokens": score.prompt_tokens,
        "completion_tokens": score.completion_tokens,
        "calibration_status": "unapproved_shadow",
        "approved_for_kernel": False,
        "provenance": score.provenance,
    }


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", required=True, type=_parse_date)
    parser.add_argument("--limit", type=_positive_int)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    args = parser.parse_args()

    candidates, candidate_snapshot = _load_candidates(args.data_root, args.trade_date)
    selected = candidates.sort("symbol")
    if args.limit is not None:
        selected = selected.head(args.limit)
    prepared, prepared_snapshot = _load_prepared_parent(
        args.data_root, candidate_snapshot
    )
    decision_asof = datetime.combine(args.trade_date, time(8, 0), BEIJING).astimezone(UTC)
    evidence = build_symbol_evidence(selected, prepared, asof_utc=decision_asof)
    client = DeepSeekClient.from_env()
    rows: list[dict[str, object]] = []
    for symbol in selected.get_column("symbol").to_list():
        score = score_intraday_continuation(
            symbol=str(symbol),
            evidence=evidence[str(symbol)],
            asof_utc=decision_asof,
            model_id=DEEPSEEK_MODEL,
            score_fn=client.score,
        )
        rows.append(_score_row(score, args.trade_date))
        print(
            json.dumps(
                {
                    "symbol": score.symbol,
                    "probability": score.probability,
                    "response_model": score.response_model,
                    "status": "unapproved_shadow",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    frame = pl.DataFrame(rows).sort("symbol")
    requested_symbols = set(selected.get_column("symbol").to_list())
    actual_symbols = set(frame.get_column("symbol").to_list())
    checks = (
        _check(
            "exact_requested_pool",
            actual_symbols == requested_symbols,
            len(actual_symbols),
            f"exactly {len(requested_symbols)} requested symbols",
        ),
        _check(
            "unique_symbol",
            frame.height == frame.get_column("symbol").n_unique(),
            frame.height - frame.get_column("symbol").n_unique(),
            "0 duplicate symbols",
        ),
        _check(
            "bounded_probability",
            frame.filter(
                (pl.col("raw_probability") < 0) | (pl.col("raw_probability") > 1)
            ).is_empty(),
            frame.filter(
                (pl.col("raw_probability") < 0) | (pl.col("raw_probability") > 1)
            ).height,
            "0 probabilities outside [0, 1]",
        ),
        _check(
            "frozen_response_model",
            frame.filter(pl.col("response_model") != DEEPSEEK_MODEL).is_empty(),
            frame.filter(pl.col("response_model") != DEEPSEEK_MODEL).height,
            f"all responses use {DEEPSEEK_MODEL}",
        ),
        _check(
            "shadow_only_before_calibration",
            frame.filter(pl.col("approved_for_kernel")).is_empty(),
            frame.filter(pl.col("approved_for_kernel")).height,
            "0 raw model scores approved for the kernel",
        ),
    )
    snapshot, path = persist_snapshot(
        frame,
        root=args.data_root,
        source="research.catalysts.deepseek_v4_pro_shadow",
        schema_version="catalyst_model_scores.v1",
        checks=checks,
        parent_snapshot_ids=(
            candidate_snapshot.dataset_id,
            prepared_snapshot.dataset_id,
        ),
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete_unapproved_shadow",
                "trade_date": args.trade_date.isoformat(),
                "rows": frame.height,
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
