"""Session-local, point-in-time research features; no policy or execution gates."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

FEATURE_VERSION = "event_features.v1"
NEW_YORK = ZoneInfo("America/New_York")
IDENTITY_COLUMNS = ("symbol", "session_id", "source", "feed", "price_basis")
BAR_COLUMNS = (
    *IDENTITY_COLUMNS, "bar_start_utc", "bar_end_utc", "available_at",
    "open", "high", "low", "close", "volume", "vwap",
)


def utc(value: datetime) -> datetime:
    """Reject naive timestamps and normalize an explicit instant to UTC."""
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("timestamps must be timezone-aware datetimes")
    return value.astimezone(UTC)


def require_text(*values: str) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("identifiers must be nonempty strings")


def finite_number(value: object, *, positive: bool = False) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value) and (value > 0 if positive else value >= 0)
    )


@dataclass(frozen=True)
class ResearchSession:
    """Caller-supplied official regular-session bounds, not a calendar lookup."""

    session_id: str
    open_utc: datetime
    close_utc: datetime

    def __post_init__(self) -> None:
        require_text(self.session_id)
        start, end = utc(self.open_utc), utc(self.close_utc)
        local_start = start.astimezone(NEW_YORK)
        local_end = end.astimezone(NEW_YORK)
        if (
            start >= end or local_start.date() != local_end.date()
            or local_start.time() != time(9, 30) or local_end.time() > time(16)
        ):
            raise ValueError("expected one regular session opening at 09:30 ET")
        object.__setattr__(self, "open_utc", start)
        object.__setattr__(self, "close_utc", end)

    @property
    def research_end_utc(self) -> datetime:
        local = self.open_utc.astimezone(NEW_YORK).replace(hour=15, minute=0)
        return min(local.astimezone(UTC), self.close_utc)


def frame_records(
    frame: pl.DataFrame, *, columns: tuple[str, ...], timestamps: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Validate structural schema, retaining null numeric observations for coverage."""
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f"missing required columns: {sorted(missing)}")
    for name in timestamps:
        dtype = frame.schema[name]
        if not isinstance(dtype, pl.Datetime) or dtype.time_zone is None:
            raise ValueError(f"{name} must have a timezone-aware Polars Datetime dtype")
    for name in IDENTITY_COLUMNS:
        if frame.schema[name] != pl.String:
            raise ValueError(f"{name} must have Polars String dtype")
    rows = frame.select(columns).to_dicts()
    # Python datetime has microsecond resolution. Round up, never round a future
    # nanosecond observation back into an eligible boundary; retain exact evidence.
    for name in timestamps:
        for row, epoch_ns in zip(
            rows, frame[name].dt.epoch("ns").to_list(), strict=True,
        ):
            if epoch_ns is None:
                raise ValueError(f"{name} must not be null")
            row[f"{name}_epoch_ns"] = epoch_ns
            row[name] = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
                microseconds=-(-epoch_ns // 1000),
            )
    for row in rows:
        require_text(*(row[name] for name in IDENTITY_COLUMNS))
        for name in timestamps:
            row[name] = utc(row[name])
    return rows


def input_hash(value: object) -> str:
    """Canonical SHA256; callers supply deterministically ordered evidence.

    This is reproducibility metadata, not authentication of mutable containers.
    """
    def encode(item: object) -> str:
        if isinstance(item, datetime):
            return utc(item).isoformat()
        raise TypeError(f"unsupported hash value: {type(item).__name__}")

    def clean(item: object) -> object:
        if isinstance(item, float) and not math.isfinite(item):
            return {"nonfinite": str(item)}
        if isinstance(item, dict):
            return {key: clean(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(val) for val in item]
        return item

    encoded = json.dumps(
        clean(value), default=encode, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def adapt_start_stamped_bars(bars: pl.DataFrame, *, interval: timedelta) -> pl.DataFrame:
    """Explicitly declare ts_utc a START stamp; available_at must already be supplied."""
    if interval not in (timedelta(minutes=1), timedelta(minutes=5)):
        raise ValueError("interval must be one or five minutes")
    if {"bar_start_utc", "bar_end_utc"} & set(bars.columns):
        raise ValueError("explicit bar bounds already exist")
    if "ts_utc" not in bars.columns or "available_at" not in bars.columns:
        raise ValueError("ts_utc and independently recorded available_at are required")
    dtype = bars.schema["ts_utc"]
    if not isinstance(dtype, pl.Datetime) or dtype.time_zone is None:
        raise ValueError("ts_utc must be timezone-aware")
    return bars.with_columns(
        pl.col("ts_utc").alias("bar_start_utc"),
        (pl.col("ts_utc") + interval).alias("bar_end_utc"),
    )


@dataclass(frozen=True)
class FeatureValue:
    value: float | bool | None
    status: str
    missing_reason: str | None
    available_at: datetime | None
    input_hash: str


@dataclass(frozen=True)
class FeatureSnapshot:
    """Record with fixed attributes but mutable dict payloads; not a signed snapshot."""

    symbol: str
    session_id: str
    decision_at: datetime
    available_at: datetime | None
    completed_through_utc: datetime
    source: str
    feed: str
    price_basis: str
    status: str
    feature_version: str
    input_hash: str
    features: dict[str, FeatureValue]
    coverage: dict[str, object]


def build_event_features(
    bars: pl.DataFrame, *, symbol: str, session: ResearchSession, decision_at: datetime,
    source: str, feed: str, price_basis: str, interval: timedelta,
    sector_symbol: str | None = None, index_symbol: str | None = None,
    sector_mapping_available_at: datetime | None = None, include_ema: bool = False,
) -> FeatureSnapshot:
    """Build completed one/five-minute features with explicit missingness.

    H15/H30 mean the opening 15/30-minute ranges. Relative returns use identical
    open-to-completed-bar windows. All features are descriptive, never hard gates.
    """
    require_text(symbol, source, feed, price_basis)
    decision_at = utc(decision_at)
    if not session.open_utc <= decision_at <= session.close_utc:
        raise ValueError("decision_at must lie within the supplied regular session")
    if interval not in (timedelta(minutes=1), timedelta(minutes=5)):
        raise ValueError("interval must be one or five minutes")
    if not isinstance(include_ema, bool):
        raise ValueError("include_ema must be bool")
    if sector_symbol is not None:
        require_text(sector_symbol)
        if sector_mapping_available_at is None:
            raise ValueError("sector mapping availability must be explicit")
    if index_symbol is not None:
        require_text(index_symbol)
    names = [name for name in (symbol, sector_symbol, index_symbol) if name is not None]
    if len(set(names)) != len(names):
        raise ValueError("stock, sector and index symbols must be distinct")
    mapping_at = utc(sector_mapping_available_at) if sector_mapping_available_at else None
    count = (min(decision_at, session.close_utc) - session.open_utc) // interval
    completed = session.open_utc + count * interval
    rows = frame_records(bars, columns=BAR_COLUMNS, timestamps=(
        "bar_start_utc", "bar_end_utc", "available_at",
    ))
    symbols = {
        value
        for value in (symbol, sector_symbol, index_symbol)
        if value is not None
    }
    selected = sorted(
        (row for row in rows if (
            row["symbol"] in symbols and row["session_id"] == session.session_id
            and (row["source"], row["feed"], row["price_basis"])
            == (source, feed, price_basis)
            and session.open_utc <= row["bar_start_utc"] < row["bar_end_utc"] <= completed
            and row["available_at"] <= decision_at
            and (
                row["symbol"] != sector_symbol
                or mapping_at is not None and mapping_at <= decision_at
            )
        )),
        key=lambda row: (row["symbol"], row["bar_start_utc"]),
    )
    by_symbol: dict[str, dict[datetime, dict[str, Any]]] = {name: {} for name in symbols}
    for row in selected:
        start, end = row["bar_start_utc"], row["bar_end_utc"]
        if (
            end - start != interval or (start - session.open_utc) % interval != timedelta(0)
            or row["bar_start_utc_epoch_ns"] % 1000 != 0
            or row["bar_end_utc_epoch_ns"] % 1000 != 0
            or row["available_at_epoch_ns"] < row["bar_end_utc_epoch_ns"]
        ):
            raise ValueError("bar bounds, grid or available_at contradict a completed bar")
        if start in by_symbol[row["symbol"]]:
            raise ValueError("duplicate bar key; resolve revisions upstream as of decision_at")
        by_symbol[row["symbol"]][start] = row

    parameters = {
        "version": FEATURE_VERSION, "session": asdict(session), "symbol": symbol,
        "decision_at": decision_at, "source": source, "feed": feed, "price_basis": price_basis,
        "interval_seconds": interval.total_seconds(), "sector_symbol": sector_symbol,
        "index_symbol": index_symbol, "sector_mapping_available_at": mapping_at,
        "include_ema": include_ema, "vwap_slope_lookback_minutes": 15,
    }
    values: dict[str, FeatureValue] = {}

    def window(
        name: str | None, first: int, last: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if name is None:
            return [], "reference_not_supplied"
        if name == sector_symbol and (mapping_at is None or mapping_at > decision_at):
            return [], "sector_mapping_unavailable"
        if last > count:
            return [], "window_incomplete"
        if first < 0 or first >= last:
            return [], "insufficient_completed_bars"
        found = [
            by_symbol[name].get(session.open_utc + i * interval)
            for i in range(first, last)
        ]
        if any(row is None for row in found):
            return [row for row in found if row is not None], "missing_bar_coverage"
        return [row for row in found if row is not None], None

    def valid_prices(evidence: list[dict[str, Any]]) -> bool:
        return all(
            all(
                finite_number(row[key], positive=True)
                for key in ("open", "high", "low", "close")
            )
            and row["low"] <= min(row["open"], row["close"])
            <= max(row["open"], row["close"]) <= row["high"]
            for row in evidence
        )

    def save(
        name: str, value: float | bool | None,
        evidence: list[dict[str, Any]], reason: str | None,
    ) -> None:
        if value is not None and not isinstance(value, bool) and not math.isfinite(value):
            value, reason = None, "nonfinite_calculation"
        if reason is not None:
            value = None
        available = max((row["available_at"] for row in evidence), default=None)
        if name in ("stock_minus_sector_return", "sector_minus_index_return") and available:
            available = max(available, mapping_at) if mapping_at else available
        values[name] = FeatureValue(
            value=value, status="available" if value is not None else "missing",
            missing_reason=reason, available_at=available if value is not None else None,
            input_hash=input_hash({
                "parameters": parameters, "feature": name, "rows": evidence,
            }),
        )

    for minutes in (15, 30):
        evidence, reason = window(symbol, 0, timedelta(minutes=minutes) // interval)
        if reason is None and not valid_prices(evidence):
            reason = "invalid_ohlc"
        for field, aggregate in (("high", max), ("low", min)):
            value = aggregate(row[field] for row in evidence) if reason is None else None
            save(f"h{minutes}_{field}", value, evidence, reason)

    current, current_reason = window(symbol, 0, count)

    def vwap(
        evidence: list[dict[str, Any]], reason: str | None,
    ) -> tuple[float | None, str | None]:
        if reason:
            return None, reason
        if not valid_prices(evidence) or any(
            not finite_number(row["volume"]) or not finite_number(row["vwap"], positive=True)
            or not row["low"] <= row["vwap"] <= row["high"] for row in evidence
        ):
            return None, "invalid_vwap_or_volume"
        volume = sum(row["volume"] for row in evidence)
        if volume <= 0:
            return None, "zero_observed_volume"
        return sum(row["vwap"] * row["volume"] for row in evidence) / volume, None

    current_vwap, vwap_reason = vwap(current, current_reason)
    save("session_vwap", current_vwap, current, vwap_reason)
    slope_lookback = timedelta(minutes=15)
    earlier, earlier_reason = window(symbol, 0, count - slope_lookback // interval)
    earlier_vwap, earlier_reason = vwap(earlier, earlier_reason)
    slope = (
        (current_vwap - earlier_vwap) / 15
        if current_vwap is not None and earlier_vwap is not None else None
    )
    save("vwap_slope_per_minute", slope, current, vwap_reason or earlier_reason)
    volume_reason = current_reason
    if not volume_reason and any(not finite_number(row["volume"]) for row in current):
        volume_reason = "invalid_volume"
    save(
        "session_volume",
        sum(row["volume"] for row in current) if not volume_reason else None,
        current, volume_reason,
    )
    save("session_dollar_volume", (
        sum(row["vwap"] * row["volume"] for row in current) if vwap_reason is None else None
    ), current, vwap_reason)

    recent, reason = window(symbol, count - 2, count)
    price_reason = reason or (None if valid_prices(recent) else "invalid_ohlc")
    for field in ("high", "low"):
        save(
            f"higher_{field}",
            recent[-1][field] > recent[-2][field] if not price_reason else None,
            recent, price_reason,
        )
    ratio_reason = reason
    if not ratio_reason and any(not finite_number(row["volume"]) for row in recent):
        ratio_reason = "invalid_volume"
    if not ratio_reason and recent[-2]["volume"] == 0:
        ratio_reason = "zero_previous_volume"
    save(
        "last_to_previous_volume",
        recent[-1]["volume"] / recent[-2]["volume"] if not ratio_reason else None,
        recent, ratio_reason,
    )

    for name, left, right in (
        ("stock_minus_sector_return", symbol, sector_symbol),
        ("sector_minus_index_return", sector_symbol, index_symbol),
    ):
        a, a_reason = window(left, 0, count)
        b, b_reason = window(right, 0, count)
        reason = a_reason or b_reason
        if not reason and not valid_prices(a + b):
            reason = "invalid_ohlc"
        value = (
            a[-1]["close"] / a[0]["open"] - b[-1]["close"] / b[0]["open"]
            if not reason else None
        )
        save(name, value, a + b, reason)

    if include_ema:
        for period in (20, 50):
            reason = current_reason or (None if valid_prices(current) else "invalid_ohlc")
            if not reason and len(current) < period:
                reason = "insufficient_same_session_ema_history"
            ema = previous = None
            if not reason:
                ema = sum(row["close"] for row in current[:period]) / period
                for row in current[period:]:
                    previous = ema
                    ema += 2 / (period + 1) * (row["close"] - ema)
            save(f"ema{period}", ema, current, reason)
            save(
                f"ema{period}_slope_per_minute",
                (ema - previous) / (interval.total_seconds() / 60)
                if ema is not None and previous is not None else None,
                current,
                reason or (
                    "insufficient_same_session_ema_history" if previous is None else None
                ),
            )

    available_at = max((v.available_at for v in values.values() if v.available_at), default=None)
    return FeatureSnapshot(
        symbol=symbol, session_id=session.session_id, decision_at=decision_at,
        available_at=available_at, completed_through_utc=completed, source=source, feed=feed,
        price_basis=price_basis,
        status="available" if all(v.status == "available" for v in values.values()) else "partial",
        feature_version=FEATURE_VERSION,
        input_hash=input_hash({"parameters": parameters, "rows": selected}),
        features=values, coverage={
            "expected_bars_per_symbol": count,
            "observed_bars_by_symbol": {name: len(by_symbol[name]) for name in sorted(symbols)},
            "missing_features": {
                name: v.missing_reason for name, v in values.items() if v.status == "missing"
            },
            "vwap_slope_lookback_minutes": 15,
            "vwap_slope_unit": "price_units_per_minute",
            "ema_role": "optional_auxiliary_not_a_gate",
        },
    )
