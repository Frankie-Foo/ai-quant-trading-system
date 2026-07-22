"""ATR and time-based long-only exit plans."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from kernel.config import Config

NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ExitPlan:
    tp_px: float
    sl_px: float
    time_stop_utc: datetime
    provenance: str


def make_exits(
    entry_px: float,
    atr14: float,
    *,
    trade_date: date,
    is_half_day: bool,
    cfg: Config,
) -> ExitPlan:
    if not math.isfinite(entry_px) or entry_px <= 0:
        raise ValueError("entry_px must be finite and positive")
    if not math.isfinite(atr14) or atr14 <= 0:
        raise ValueError("atr14 must be finite and positive")
    clock_text = "12:55" if is_half_day else cfg.exits.time_stop_et
    hour, minute = (int(part) for part in clock_text.split(":"))
    stop_local = datetime.combine(trade_date, time(hour, minute), NEW_YORK)
    return ExitPlan(
        tp_px=entry_px + cfg.exits.k_tp * atr14,
        sl_px=entry_px - cfg.exits.k_sl * atr14,
        time_stop_utc=stop_local.astimezone(UTC),
        provenance=(
            f"kernel.exits.make_exits@{trade_date.isoformat()}|"
            f"tp={cfg.exits.k_tp}atr|sl={cfg.exits.k_sl}atr|stop={clock_text}ET"
        ),
    )
