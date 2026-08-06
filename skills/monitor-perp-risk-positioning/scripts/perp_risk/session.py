"""XNYS-aware actionable-window evaluation."""

from __future__ import annotations

from datetime import datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from .config import SessionConfig
from .models import require_utc


def session_state(
    asof_utc: datetime,
    config: SessionConfig,
) -> tuple[
    bool,
    Literal["actionable", "research_only", "market_closed"],
]:
    require_utc(asof_utc, name="asof_utc")
    timezone = ZoneInfo(config.timezone)
    local = asof_utc.astimezone(timezone)
    calendar_name = (
        "NYSE" if config.exchange_calendar.upper() == "XNYS" else config.exchange_calendar
    )
    calendar = mcal.get_calendar(calendar_name)
    date_string = local.date().isoformat()
    schedule = calendar.schedule(start_date=date_string, end_date=date_string)
    if schedule.empty:
        return False, "market_closed"
    hour, minute = (int(item) for item in config.actionable_start.split(":"))
    start = datetime.combine(
        local.date(),
        time(hour=hour, minute=minute),
        tzinfo=timezone,
    )
    close = schedule.iloc[0]["market_close"].to_pydatetime()
    close = close.astimezone(timezone)
    if start <= local <= close:
        return True, "actionable"
    return False, "research_only"
