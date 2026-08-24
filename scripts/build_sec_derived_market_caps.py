"""Derive point-in-time market caps from SEC shares and raw Alpaca closes."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, QualitySeverity
from data_plane.http import get_json
from data_plane.providers.alpaca import fetch_daily_bars, stock_data_policy_from_env
from data_plane.providers.sec_filings import sec_user_agent
from data_plane.storage import persist_snapshot
from operations.local_env import load_project_env, project_data_root

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "sec.companyfacts.derived_market_cap"
SHARE_TAGS = (
    "EntityCommonStockSharesOutstanding",
    "CommonStockSharesOutstanding",
    "CommonStocksIncludingAdditionalPaidInCapitalMember",
)
FORMS = {"10-Q", "10-Q/A", "10-K", "10-K/A", "20-F", "20-F/A", "40-F"}


def _headers() -> dict[str, str]:
    return {"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"}


def _ticker_ciks() -> dict[str, str]:
    payload = get_json("https://www.sec.gov/files/company_tickers.json", headers=_headers())
    return {
        str(value.get("ticker", "")).upper(): str(value.get("cik_str", "")).zfill(10)
        for value in payload.values()
        if isinstance(value, dict) and value.get("ticker") and value.get("cik_str")
    }


def _fetch_facts(cik: str) -> tuple[str, dict[str, Any]]:
    payload = get_json(
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
        headers=_headers(),
    )
    return cik, payload


def _shares_asof(
    payload: dict[str, Any], asof_date: date
) -> tuple[float, date, date, str] | None:
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        return None
    candidates: list[tuple[date, date, float, str]] = []
    for namespace in facts.values():
        if not isinstance(namespace, dict):
            continue
        for tag in SHARE_TAGS:
            concept = namespace.get(tag)
            if not isinstance(concept, dict):
                continue
            units = concept.get("units")
            if not isinstance(units, dict):
                continue
            for unit_name, entries in units.items():
                if "share" not in str(unit_name).lower() or not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict) or entry.get("form") not in FORMS:
                        continue
                    try:
                        filed = date.fromisoformat(str(entry["filed"]))
                        end = date.fromisoformat(str(entry["end"]))
                        value = float(entry["val"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if filed <= asof_date and end <= asof_date and value > 0:
                        candidates.append((filed, end, value, tag))
    if not candidates:
        return None
    filed, end, value, tag = max(candidates, key=lambda item: (item[0], item[1]))
    return value, filed, end, tag


def _checks(frame: pl.DataFrame) -> tuple[DataQualityCheck, ...]:
    duplicates = frame.height - frame.select(
        pl.struct("asof_date", "symbol").n_unique()
    ).item()
    future = frame.filter(
        (pl.col("fact_filed_date") > pl.col("asof_date"))
        | (pl.col("fact_end_date") > pl.col("asof_date"))
    ).height
    invalid = frame.filter(
        (pl.col("market_cap") <= 0)
        | (pl.col("shares_outstanding") <= 0)
        | (pl.col("raw_close") <= 0)
    ).height

    def check(name: str, passed: bool, observed: object, expected: str) -> DataQualityCheck:
        return DataQualityCheck(
            name=name,
            severity=QualitySeverity.CRITICAL,
            passed=passed,
            observed=str(observed),
            expected=expected,
            provenance=SOURCE,
        )

    return (
        check("non_empty", frame.height > 0, frame.height, ">0"),
        check("unique_symbol_asof", duplicates == 0, duplicates, "0"),
        check("no_future_fact", future == 0, future, "0"),
        check("positive_inputs", invalid == 0, invalid, "0"),
    )


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signals", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    args = parser.parse_args()
    signals = pl.read_parquet(args.signals)
    targets = sorted(signals.get_column("session_date").unique().to_list())
    sessions = build_xnys_schedule(targets[0] - timedelta(days=10), targets[-1]).get_column(
        "trade_date"
    ).to_list()
    previous = {
        target: max(item for item in sessions if item < target) for target in targets
    }
    requests = sorted(
        {
            (previous[row["session_date"]], str(row["symbol"]))
            for row in signals.select("session_date", "symbol").iter_rows(named=True)
        }
    )
    ticker_ciks = _ticker_ciks()
    ciks = sorted({ticker_ciks[symbol] for _, symbol in requests if symbol in ticker_ciks})
    facts: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_fetch_facts, cik): cik for cik in ciks}
        for completed, future in enumerate(as_completed(futures), start=1):
            cik, payload = future.result()
            facts[cik] = payload
            if completed % 20 == 0 or completed == len(ciks):
                print(
                    json.dumps({"event": "sec_facts", "completed": completed, "total": len(ciks)}),
                    flush=True,
                )

    symbols = tuple(sorted({symbol for _, symbol in requests}))
    start_utc = datetime.combine(min(item[0] for item in requests), datetime.min.time(), UTC)
    end_utc = datetime.combine(
        max(item[0] for item in requests) + timedelta(days=1),
        datetime.min.time(),
        UTC,
    )
    raw = fetch_daily_bars(
        symbols,
        start_utc,
        end_utc,
        feed=stock_data_policy_from_env().feed,
        adjustment="raw",
    ).with_columns(
        pl.col("ts_utc")
        .dt.convert_time_zone("America/New_York")
        .dt.date()
        .alias("asof_date")
    )
    closes = {
        (row["asof_date"], row["symbol"]): float(row["close"])
        for row in raw.select("asof_date", "symbol", "close").iter_rows(named=True)
    }
    rows: list[dict[str, object]] = []
    missing: list[dict[str, str]] = []
    for asof_date, symbol in requests:
        signal_cik = ticker_ciks.get(symbol)
        raw_close = closes.get((asof_date, symbol))
        fact = (
            _shares_asof(facts[signal_cik], asof_date)
            if signal_cik in facts
            else None
        )
        if signal_cik is None or raw_close is None or fact is None:
            missing.append({"asof_date": asof_date.isoformat(), "symbol": symbol})
            continue
        shares, filed, end, tag = fact
        rows.append(
            {
                "asof_date": asof_date,
                "symbol": symbol,
                "market_cap": shares * raw_close,
                "shares_outstanding": shares,
                "raw_close": raw_close,
                "cik": signal_cik,
                "fact_filed_date": filed,
                "fact_end_date": end,
                "fact_tag": tag,
                "source": SOURCE,
                "provenance": (
                    f"sec.companyfacts:CIK{signal_cik}:{tag}@filed={filed.isoformat()}|"
                    f"alpaca.sip.raw.close@{asof_date.isoformat()}"
                ),
            }
        )
    frame = pl.DataFrame(rows, infer_schema_length=None)
    snapshot, path = persist_snapshot(
        frame,
        root=args.data_root,
        source=SOURCE,
        schema_version="derived_market_cap.v1",
        checks=_checks(frame),
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "requests": len(requests),
                "derived": frame.height,
                "missing": missing,
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
