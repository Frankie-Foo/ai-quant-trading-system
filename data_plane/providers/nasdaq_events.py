from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from data_plane.http import DownloadError, get_json, get_response

EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"
TRADE_HALTS_URL = "https://www.nasdaqtrader.com/rss.aspx"
NEW_YORK = ZoneInfo("America/New_York")
NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; trading-system-v2 research)",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}


def _money(value: object) -> float | None:
    text = str(value or "").strip().replace("$", "").replace(",", "")
    if not text or text.upper() == "N/A":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _timing(value: object) -> str:
    mapping = {
        "time-pre-market": "pre_market",
        "time-after-hours": "after_market",
        "time-not-supplied": "not_supplied",
    }
    return mapping.get(str(value or "").strip(), "not_supplied")


def _parse_earnings_payload(
    payload: dict[str, Any], *, trade_date: date, retrieved_utc: datetime
) -> pl.DataFrame:
    data = payload.get("data")
    raw_rows = data.get("rows", []) if isinstance(data, dict) else []
    if not isinstance(raw_rows, list):
        raise DownloadError("Nasdaq earnings response rows are not a list")
    rows: list[dict[str, object]] = []
    for item in raw_rows:
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        rows.append(
            {
                "trade_date": trade_date,
                "symbol": str(item["symbol"]).strip().upper(),
                "company_name": item.get("name"),
                "release_timing": _timing(item.get("time")),
                "provider_market_cap": _money(item.get("marketCap")),
                "fiscal_quarter_ending": item.get("fiscalQuarterEnding"),
                "eps_forecast": item.get("epsForecast"),
                "estimate_count": item.get("noOfEsts"),
                "retrieved_utc": retrieved_utc,
                "source": "nasdaq.earnings_calendar.zacks",
                "provenance": (
                    f"nasdaq.earnings_calendar@{trade_date.isoformat()}|"
                    "provider=Zacks|algorithmic_expected_date"
                ),
            }
        )
    return (
        pl.DataFrame(rows, schema=_earnings_schema())
        .unique(subset=["trade_date", "symbol"], keep="last")
        .sort("symbol")
    )


def fetch_earnings_calendar(trade_date: date) -> pl.DataFrame:
    retrieved_utc = datetime.now(UTC)
    payload = get_json(
        EARNINGS_URL,
        params={"date": trade_date.isoformat()},
        headers=NASDAQ_HEADERS,
    )
    return _parse_earnings_payload(
        payload, trade_date=trade_date, retrieved_utc=retrieved_utc
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1]


def _event_timestamp(date_text: object, time_text: object) -> datetime | None:
    raw_date = str(date_text or "").strip()
    raw_time = str(time_text or "").strip()
    if not raw_date or not raw_time or raw_date.upper() == "N/A":
        return None
    match = re.search(r"(\d{2}:\d{2}:\d{2})(?:\s*\.\s*(\d+))?", raw_time)
    if match is None:
        return None
    parsed_date = datetime.strptime(raw_date, "%m/%d/%Y").date()
    parsed_time = datetime.strptime(match.group(1), "%H:%M:%S").time()
    microseconds = int((match.group(2) or "0")[:6].ljust(6, "0"))
    local = datetime.combine(parsed_date, parsed_time, NEW_YORK).replace(
        microsecond=microseconds
    )
    return local.astimezone(UTC)


def _parse_trade_halt_xml(content: bytes, *, retrieved_utc: datetime) -> pl.DataFrame:
    try:
        root = ET.fromstring(content.lstrip(b"\xef\xbb\xbf"))
    except ET.ParseError as exc:
        raise DownloadError("Nasdaq Trader halt response is invalid XML") from exc
    rows: list[dict[str, object]] = []
    for item in root.findall(".//item"):
        values = {
            _local_name(child.tag): (child.text or "").strip() for child in item
        }
        symbol = values.get("IssueSymbol", values.get("title", "")).strip().upper()
        if not symbol:
            continue
        halt_ts = _event_timestamp(values.get("HaltDate"), values.get("HaltTime"))
        if halt_ts is None:
            continue
        resumption_ts = _event_timestamp(
            values.get("ResumptionDate"), values.get("ResumptionTradeTime")
        )
        reason_code = values.get("ReasonCode", "").strip().upper()
        rows.append(
            {
                "symbol": symbol,
                "issue_name": values.get("IssueName") or None,
                "market_code": values.get("Mkt") or None,
                "reason_code": reason_code,
                "halt_date": halt_ts.astimezone(NEW_YORK).date(),
                "halt_ts_utc": halt_ts,
                "resumption_ts_utc": resumption_ts,
                "is_luld": reason_code in {"LUDP", "LUDS"},
                "retrieved_utc": retrieved_utc,
                "source": "nasdaqtrader.trade_halts",
                "provenance": (
                    f"nasdaqtrader.trade_halts:{symbol}:"
                    f"{halt_ts.isoformat()}:{reason_code}"
                ),
            }
        )
    return pl.DataFrame(rows, schema=_halt_schema()).sort("halt_ts_utc", "symbol")


def fetch_trade_halts(halt_dates: list[date]) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    current_new_york_date = datetime.now(NEW_YORK).date()
    for halt_date in sorted(set(halt_dates)):
        params = {"feed": "tradehalts"}
        if halt_date != current_new_york_date:
            params["haltdate"] = halt_date.strftime("%m%d%Y")
        last_error: DownloadError | None = None
        for attempt in range(4):
            response = get_response(
                TRADE_HALTS_URL,
                params=params,
                headers={"User-Agent": NASDAQ_HEADERS["User-Agent"]},
            )
            try:
                frame = _parse_trade_halt_xml(
                    response.content, retrieved_utc=datetime.now(UTC)
                )
                frames.append(frame)
                last_error = None
                break
            except DownloadError as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(float(2**attempt))
        if last_error is not None:
            raise DownloadError(
                f"Nasdaq Trader returned invalid XML for {halt_date.isoformat()}"
            ) from last_error
    if not frames:
        return pl.DataFrame(schema=_halt_schema())
    return (
        pl.concat(frames)
        .sort("retrieved_utc")
        .unique(subset=["symbol", "halt_ts_utc"], keep="last")
        .sort("halt_ts_utc", "symbol")
    )


def _earnings_schema() -> dict[str, Any]:
    return {
        "trade_date": pl.Date,
        "symbol": pl.String,
        "company_name": pl.String,
        "release_timing": pl.String,
        "provider_market_cap": pl.Float64,
        "fiscal_quarter_ending": pl.String,
        "eps_forecast": pl.String,
        "estimate_count": pl.String,
        "retrieved_utc": pl.Datetime("ms", "UTC"),
        "source": pl.String,
        "provenance": pl.String,
    }


def _halt_schema() -> dict[str, Any]:
    return {
        "symbol": pl.String,
        "issue_name": pl.String,
        "market_code": pl.String,
        "reason_code": pl.String,
        "halt_date": pl.Date,
        "halt_ts_utc": pl.Datetime("ms", "UTC"),
        "resumption_ts_utc": pl.Datetime("ms", "UTC"),
        "is_luld": pl.Boolean,
        "retrieved_utc": pl.Datetime("ms", "UTC"),
        "source": pl.String,
        "provenance": pl.String,
    }
