from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.catalysts import (
    CATALYST_SCHEMA_VERSION,
    audit_catalysts,
    empty_catalyst_frame,
)
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.providers.catalyst_news import (
    fetch_alpaca_news,
    fetch_alpaca_news_direct,
    fetch_massive_news,
)
from data_plane.providers.sec_filings import (
    fetch_candidate_filings,
    fetch_live_candidate_filings,
)
from data_plane.storage import persist_snapshot
from kernel.catalysts import (
    build_catalyst_candidates,
    prepare_catalysts,
    select_overnight_catalysts,
)
from operations.local_env import load_project_env

ROOT = Path(__file__).resolve().parents[1]
BEIJING = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("asof must include a timezone")
    return parsed.astimezone(UTC)


def _manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"manifest is not an object: {path}")
    return value


def _dataset_id(path: Path) -> str:
    value = _manifest(path.parent / "manifest.json").get("dataset_id")
    if not isinstance(value, str):
        raise ValueError(f"manifest has no dataset_id: {path}")
    return value


def _load_universe(
    data_root: Path, previous_session: date
) -> tuple[pl.DataFrame, str]:
    matches: list[tuple[datetime, Path]] = []
    for path in (data_root / "accepted").glob(
        "kernel.universe.daily_precheck-*/data.parquet"
    ):
        frame = pl.read_parquet(path, columns=["asof_date"])
        asof_date = frame.get_column("asof_date").max()
        if asof_date != previous_session:
            continue
        manifest = _manifest(path.parent / "manifest.json")
        asof_utc = datetime.fromisoformat(str(manifest["asof_utc"]).replace("Z", "+00:00"))
        matches.append((asof_utc, path))
    if not matches:
        raise FileNotFoundError(
            f"no accepted universe snapshot with asof_date={previous_session}"
        )
    path = max(matches)[1]
    return pl.read_parquet(path), _dataset_id(path)


def _load_reference_map(
    data_root: Path,
    *,
    previous_session: date,
    universe: pl.DataFrame,
) -> tuple[dict[str, tuple[str, ...]], str]:
    matches: list[tuple[date, Path]] = []
    for path in (data_root / "accepted").glob(
        "massive.reference_tickers.cs-*/data.parquet"
    ):
        value = pl.read_parquet(path, columns=["asof_date"]).get_column("asof_date").max()
        if isinstance(value, date) and value <= previous_session:
            matches.append((value, path))
    if not matches:
        raise FileNotFoundError("no accepted Massive common-stock reference snapshot")
    reference_date, path = max(matches)
    if reference_date != previous_session:
        raise ValueError(
            f"latest common-stock reference is {reference_date}, expected {previous_session}"
        )
    reference = pl.read_parquet(path, columns=["symbol", "cik"])
    eligible = universe.filter(pl.col("precheck_pass")).select("symbol")
    joined = reference.join(eligible, on="symbol", how="inner").filter(
        pl.col("cik").is_not_null() & (pl.col("cik").str.len_chars() > 0)
    )
    mapping: dict[str, tuple[str, ...]] = {}
    for row in joined.group_by("cik").agg(pl.col("symbol").sort()).iter_rows(named=True):
        symbols = row["symbol"]
        if isinstance(symbols, list):
            mapping[str(row["cik"])] = tuple(str(item) for item in symbols)
    return mapping, _dataset_id(path)


def _load_locked_candidates(
    data_root: Path, target_date: date
) -> tuple[set[str], str]:
    matches: list[tuple[datetime, Path]] = []
    for path in (data_root / "accepted").glob(
        "kernel.catalysts.overnight_candidates-*/data.parquet"
    ):
        frame = pl.read_parquet(path, columns=["session_date"])
        dates = frame.get_column("session_date").unique().to_list()
        if dates != [target_date]:
            continue
        snapshot = DatasetSnapshot.model_validate(_manifest(path.parent / "manifest.json"))
        matches.append((snapshot.asof_utc, path))
    if not matches:
        raise FileNotFoundError(f"no locked catalyst candidate snapshot for {target_date}")
    path = max(matches)[1]
    symbols = set(pl.read_parquet(path, columns=["symbol"]).get_column("symbol").to_list())
    return symbols, _dataset_id(path)


def _store_provider(
    frame: pl.DataFrame,
    *,
    data_root: Path,
    source: str,
    start_utc: datetime,
    end_utc: datetime,
    require_non_empty: bool,
    parent_snapshot_ids: tuple[str, ...] = (),
) -> DatasetSnapshot:
    checks = audit_catalysts(
        frame,
        provenance=f"{source}@{start_utc.isoformat()}..{end_utc.isoformat()}",
        start_utc=start_utc,
        end_utc=end_utc,
        require_non_empty=require_non_empty,
    )
    snapshot, _ = persist_snapshot(
        frame,
        root=data_root,
        source=source,
        schema_version=CATALYST_SCHEMA_VERSION,
        checks=checks,
        parent_snapshot_ids=parent_snapshot_ids,
    )
    snapshot.assert_usable()
    return snapshot


def _load_provider_snapshot(
    data_root: Path,
    *,
    source: str,
    start_utc: datetime,
    end_utc: datetime,
) -> tuple[pl.DataFrame, DatasetSnapshot]:
    expected_provenance = f"{source}@{start_utc.isoformat()}..{end_utc.isoformat()}"
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(f"{source}-*/data.parquet"):
        payload = _manifest(path.parent / "manifest.json")
        checks = payload.get("checks", [])
        if not isinstance(checks, list) or not any(
            isinstance(check, dict) and check.get("provenance") == expected_provenance
            for check in checks
        ):
            continue
        snapshot = DatasetSnapshot.model_validate(payload)
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        raise FileNotFoundError(
            f"no accepted {source} snapshot for {expected_provenance}"
        )
    _, path, snapshot = max(matches)
    return pl.read_parquet(path), snapshot


def _check(
    name: str,
    severity: QualitySeverity,
    passed: bool,
    observed: object,
    expected: str,
    provenance: str,
) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=severity,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance=provenance,
    )


def _prepared_checks(
    frame: pl.DataFrame, *, target_date: date, asof_utc: datetime
) -> tuple[DataQualityCheck, ...]:
    provenance = f"kernel.catalysts.prepare@{target_date.isoformat()}"
    future_eligible = frame.filter(
        pl.col("eligible") & (pl.col("published_utc") > asof_utc)
    ).height
    invalid_broad = frame.filter(pl.col("eligible") & (pl.col("symbol_count") > 3)).height
    invalid_short = frame.filter(
        pl.col("eligible")
        & (pl.col("event_type") == "news")
        & (pl.col("word_count") < 25)
        & ~pl.col("earnings_structured")
    ).height
    duplicate_eligible = frame.filter(pl.col("eligible")).select(
        pl.col("content_fingerprint").is_duplicated().sum()
    ).item()
    nonnull_model_scores = frame.get_column("model_score").is_not_null().sum()
    return (
        _check(
            "no_future_eligible_events",
            QualitySeverity.CRITICAL,
            future_eligible == 0,
            future_eligible,
            "0",
            provenance,
        ),
        _check(
            "phase1_cleaning_rules",
            QualitySeverity.CRITICAL,
            invalid_broad + invalid_short == 0,
            invalid_broad + invalid_short,
            "0 eligible broad or unstructured short news items",
            provenance,
        ),
        _check(
            "one_eligible_record_per_event_chain",
            QualitySeverity.CRITICAL,
            duplicate_eligible == 0,
            duplicate_eligible,
            "0 duplicate eligible fingerprints",
            provenance,
        ),
        _check(
            "uncalibrated_model_score_absent",
            QualitySeverity.CRITICAL,
            nonnull_model_scores == 0,
            nonnull_model_scores,
            "0 model scores before calibration",
            provenance,
        ),
    )


def _candidate_checks(
    frame: pl.DataFrame, *, target_date: date, universe: pl.DataFrame
) -> tuple[DataQualityCheck, ...]:
    provenance = f"kernel.catalysts.candidates@{target_date.isoformat()}"
    duplicates = frame.height - frame.get_column("symbol").n_unique()
    invalid_counts = frame.filter(
        (pl.col("event_count") < 1) | (pl.col("independent_source_count") < 1)
    ).height
    universe_symbols = set(
        universe.filter(pl.col("precheck_pass")).get_column("symbol").to_list()
    )
    outside_universe = sum(
        symbol not in universe_symbols for symbol in frame.get_column("symbol").to_list()
    )
    model_scores = frame.get_column("model_score").is_not_null().sum()
    return (
        _check(
            "non_empty_candidates",
            QualitySeverity.INFO,
            frame.height > 0,
            frame.height,
            "zero is allowed; no catalyst is a valid outcome",
            provenance,
        ),
        _check(
            "unique_candidate_symbol",
            QualitySeverity.CRITICAL,
            duplicates == 0,
            duplicates,
            "0 duplicate symbols",
            provenance,
        ),
        _check(
            "daily_precheck_membership",
            QualitySeverity.CRITICAL,
            outside_universe == 0,
            outside_universe,
            "0 symbols outside daily precheck",
            provenance,
        ),
        _check(
            "positive_evidence_counts",
            QualitySeverity.CRITICAL,
            invalid_counts == 0,
            invalid_counts,
            "all candidates have at least one event and source",
            provenance,
        ),
        _check(
            "uncalibrated_model_score_absent",
            QualitySeverity.CRITICAL,
            model_scores == 0,
            model_scores,
            "0 model scores before calibration",
            provenance,
        ),
    )


def _filing_dates(start_utc: datetime, end_utc: datetime) -> list[date]:
    current = start_utc.astimezone(NEW_YORK).date()
    last = end_utc.astimezone(NEW_YORK).date()
    values: list[date] = []
    while current <= last:
        if current.weekday() < 5:
            values.append(current)
        current += timedelta(days=1)
    return values


def _counts(frame: pl.DataFrame, column: str) -> dict[str, int]:
    if frame.is_empty():
        return {}
    output: dict[str, int] = {}
    for row in frame.group_by(column).len().sort("len", descending=True).iter_rows(named=True):
        output[str(row[column])] = int(row["len"])
    return output


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_parse_date, required=True)
    parser.add_argument("--asof", type=_parse_utc)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--massive-pace-seconds", type=float, default=12.5)
    parser.add_argument("--reuse-provider-snapshots", action="store_true")
    args = parser.parse_args()

    default_asof = datetime.combine(args.trade_date, time(8, 0), BEIJING).astimezone(UTC)
    asof_utc: datetime = args.asof or default_asof
    verification_mode = asof_utc > default_asof
    schedule = build_xnys_schedule(args.trade_date - timedelta(days=10), args.trade_date)
    target = schedule.filter(pl.col("trade_date") == args.trade_date)
    previous = schedule.filter(pl.col("trade_date") < args.trade_date).sort("trade_date").tail(1)
    if target.height != 1 or previous.height != 1:
        raise ValueError("target or previous XNYS session is unavailable")
    previous_session = previous.get_column("trade_date")[0]
    start_utc = previous.get_column("market_close_utc")[0]
    if not isinstance(previous_session, date) or not isinstance(start_utc, datetime):
        raise ValueError("calendar values have invalid types")
    if asof_utc <= start_utc:
        raise ValueError("catalyst asof must be after the previous session close")
    end_utc = asof_utc + timedelta(seconds=1)

    universe, universe_snapshot_id = _load_universe(args.data_root, previous_session)
    locked_snapshot_id: str | None = None
    candidate_universe = universe
    if verification_mode:
        locked_symbols, locked_snapshot_id = _load_locked_candidates(
            args.data_root, args.trade_date
        )
        candidate_universe = universe.with_columns(
            (
                pl.col("precheck_pass")
                & pl.col("symbol").is_in(sorted(locked_symbols))
            ).alias("precheck_pass")
        )
    cik_map, reference_snapshot_id = _load_reference_map(
        args.data_root, previous_session=previous_session, universe=candidate_universe
    )

    if args.reuse_provider_snapshots:
        alpaca, alpaca_snapshot = _load_provider_snapshot(
            args.data_root,
            source="alpaca.news.benzinga",
            start_utc=start_utc,
            end_utc=end_utc,
        )
        massive, massive_snapshot = _load_provider_snapshot(
            args.data_root,
            source="massive.news",
            start_utc=start_utc,
            end_utc=end_utc,
        )
        sec, sec_snapshot = _load_provider_snapshot(
            args.data_root,
            source="sec.submissions.candidate_filings",
            start_utc=start_utc,
            end_utc=end_utc,
        )
    else:
        market_provider = os.getenv("DESKTOP_MARKET_DATA_PROVIDER", "").strip().lower()
        standalone = market_provider in {
            "local_massive",
            "alpaca_proxy_rest",
        }
        if market_provider == "alpaca_direct":
            direct_symbols = tuple(
                candidate_universe
                .filter(pl.col("precheck_pass"))
                .get_column("symbol")
                .to_list()
            )
            alpaca = fetch_alpaca_news_direct(
                start_utc,
                end_utc,
                symbols=direct_symbols,
            )
        elif standalone:
            alpaca = empty_catalyst_frame()
        else:
            alpaca = fetch_alpaca_news(start_utc, end_utc)
        massive = fetch_massive_news(
            start_utc, end_utc, pace_seconds=args.massive_pace_seconds
        )
        if verification_mode:
            sec = fetch_live_candidate_filings(
                cik_to_symbols=cik_map,
                start_utc=start_utc,
                end_utc=end_utc,
            )
        else:
            sec = fetch_candidate_filings(
                _filing_dates(start_utc, asof_utc),
                cik_to_symbols=cik_map,
                start_utc=start_utc,
                end_utc=end_utc,
            )

        alpaca_snapshot = _store_provider(
            alpaca,
            data_root=args.data_root,
            source="alpaca.news.benzinga",
            start_utc=start_utc,
            end_utc=end_utc,
            require_non_empty=not standalone,
        )
        massive_snapshot = _store_provider(
            massive,
            data_root=args.data_root,
            source="massive.news",
            start_utc=start_utc,
            end_utc=end_utc,
            require_non_empty=True,
        )
        sec_parents = [reference_snapshot_id, universe_snapshot_id]
        if locked_snapshot_id is not None:
            sec_parents.append(locked_snapshot_id)
        sec_snapshot = _store_provider(
            sec,
            data_root=args.data_root,
            source="sec.submissions.candidate_filings",
            start_utc=start_utc,
            end_utc=end_utc,
            require_non_empty=False,
            parent_snapshot_ids=tuple(sec_parents),
        )

    raw = pl.concat((alpaca, massive, sec))
    prepared = prepare_catalysts(raw, asof_utc=asof_utc)
    prepared_checks = _prepared_checks(
        prepared, target_date=args.trade_date, asof_utc=asof_utc
    )
    prepared_snapshot, _ = persist_snapshot(
        prepared,
        root=args.data_root,
        source="kernel.catalysts.prepared",
        schema_version="prepared_catalysts.v2",
        checks=prepared_checks,
        parent_snapshot_ids=(
            alpaca_snapshot.dataset_id,
            massive_snapshot.dataset_id,
            sec_snapshot.dataset_id,
        ),
    )
    prepared_snapshot.assert_usable()

    overnight = select_overnight_catalysts(
        prepared,
        schedule=schedule,
        target_date=args.trade_date,
        asof_utc=asof_utc,
    )
    candidates = build_catalyst_candidates(candidate_universe, overnight)
    candidate_checks = _candidate_checks(
        candidates, target_date=args.trade_date, universe=candidate_universe
    )
    candidate_source = (
        "kernel.catalysts.locked_verification_candidates"
        if verification_mode
        else "kernel.catalysts.overnight_candidates"
    )
    candidate_parents = [prepared_snapshot.dataset_id, universe_snapshot_id]
    if locked_snapshot_id is not None:
        candidate_parents.append(locked_snapshot_id)
    candidate_snapshot, candidate_path = persist_snapshot(
        candidates,
        root=args.data_root,
        source=candidate_source,
        schema_version="overnight_catalyst_candidates.v2",
        checks=candidate_checks,
        parent_snapshot_ids=tuple(candidate_parents),
    )
    candidate_snapshot.assert_usable()

    result = {
        "trade_date": args.trade_date.isoformat(),
        "mode": "locked_verification" if verification_mode else "pool_lock",
        "locked_symbols": (
            candidate_universe.filter(pl.col("precheck_pass")).height
            if verification_mode
            else None
        ),
        "window_start_utc": start_utc.isoformat(),
        "asof_utc": asof_utc.isoformat(),
        "provider_rows": {
            "alpaca": alpaca.height,
            "massive": massive.height,
            "sec": sec.height,
        },
        "prepared_rows": prepared.height,
        "eligible_overnight_events": overnight.height,
        "exclusions": _counts(
            prepared.filter(pl.col("exclude_reason").is_not_null()), "exclude_reason"
        ),
        "categories": _counts(overnight, "catalyst_category"),
        "candidate_symbols": candidates.height,
        "candidate_dataset_id": candidate_snapshot.dataset_id,
        "candidate_path": str(candidate_path),
        "failed_checks": [
            check.name
            for check in (*prepared_checks, *candidate_checks)
            if not check.passed and check.severity is QualitySeverity.CRITICAL
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
