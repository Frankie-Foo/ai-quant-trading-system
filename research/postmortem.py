"""Point-in-time postmarket episode construction for the slow research loop."""

from __future__ import annotations

from datetime import date, datetime

import polars as pl

from kernel.config import Config
from kernel.exits import make_exits
from kernel.labels import triple_barrier

EPISODE_SCHEMA_VERSION = "trading_episode.v1"


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _list_of_text(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def build_trading_episode(
    *,
    selection: pl.DataFrame,
    signals: pl.DataFrame,
    bars: pl.DataFrame,
    catalyst_scores: pl.DataFrame,
    trade_date: date,
    session_open_utc: datetime,
    session_close_utc: datetime,
    is_half_day: bool,
    cfg: Config,
) -> pl.DataFrame:
    """Join frozen decision facts to realized outcomes without inventing costs."""
    if session_open_utc.tzinfo is None or session_close_utc.tzinfo is None:
        raise ValueError("session timestamps must be timezone-aware")
    if session_close_utc <= session_open_utc:
        raise ValueError("session close must be after session open")
    required_selection = {
        "symbol",
        "session_date",
        "selection_rank",
        "pass_gate",
        "rvol",
        "price",
        "adv_usd",
        "market_cap",
        "tier",
        "beta",
        "atr_pct",
        "event_count",
        "catalyst_categories",
        "evidence_event_ids",
        "evidence_sources",
    }
    required_signals = {
        "symbol",
        "triggered",
        "reason",
        "opening_range_high",
        "opening_range_low",
        "opening_range_open",
        "opening_range_close",
        "trigger_ts_utc",
        "entry_ts_utc",
        "entry_px",
        "provenance",
    }
    required_bars = {"symbol", "ts_utc", "open", "high", "low", "close"}
    for name, frame, required in (
        ("selection", selection, required_selection),
        ("signals", signals, required_signals),
        ("bars", bars, required_bars),
    ):
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"{name} missing required columns: {sorted(missing)}")

    survivors = selection.filter(pl.col("pass_gate")).sort("selection_rank", "symbol")
    if survivors.filter(pl.col("session_date") != trade_date).height:
        raise ValueError("selection contains a different session date")
    survivor_symbols = survivors.get_column("symbol").to_list()
    signal_symbols = signals.get_column("symbol").to_list()
    if len(signal_symbols) != len(set(signal_symbols)):
        raise ValueError("signals contain duplicate symbols")
    if set(signal_symbols) != set(survivor_symbols):
        raise ValueError("signals do not exactly match gate survivors")
    if "approved_for_kernel" in catalyst_scores.columns and catalyst_scores.filter(
        pl.col("approved_for_kernel")
    ).height:
        raise ValueError("postmarket review received an unexpectedly approved raw score")

    signal_map = {str(row["symbol"]): row for row in signals.iter_rows(named=True)}
    score_map = {
        str(row["symbol"]): row for row in catalyst_scores.iter_rows(named=True)
    }
    rows: list[dict[str, object]] = []
    for candidate in survivors.iter_rows(named=True):
        symbol = str(candidate["symbol"])
        signal = signal_map[symbol]
        score = score_map.get(symbol, {})
        symbol_bars = bars.filter(
            (pl.col("symbol") == symbol)
            & (pl.col("ts_utc") >= session_open_utc)
            & (pl.col("ts_utc") < session_close_utc)
        ).sort("ts_utc")
        prior_close = _number(candidate.get("price"))
        rth_open = _number(symbol_bars.get_column("open")[0]) if symbol_bars.height else None
        rth_high = _number(symbol_bars.get_column("high").max())
        rth_low = _number(symbol_bars.get_column("low").min())
        rth_close = (
            _number(symbol_bars.get_column("close")[-1]) if symbol_bars.height else None
        )
        rth_high_return = (
            rth_high / prior_close - 1
            if rth_high is not None and prior_close is not None and prior_close > 0
            else None
        )
        rth_close_return = (
            rth_close / prior_close - 1
            if rth_close is not None and prior_close is not None and prior_close > 0
            else None
        )

        triggered = bool(signal["triggered"])
        outcome_label = "no_trigger"
        outcome_status = "complete"
        outcome_detail: str | None = None
        exit_ts_utc: datetime | None = None
        exit_px: float | None = None
        gross_return: float | None = None
        outcome_provenance: str | None = None
        if triggered:
            entry_ts = signal.get("entry_ts_utc")
            entry_px = _number(signal.get("entry_px"))
            atr_pct = _number(candidate.get("atr_pct"))
            if (
                not isinstance(entry_ts, datetime)
                or entry_px is None
                or prior_close is None
                or atr_pct is None
            ):
                outcome_label = "unavailable"
                outcome_status = "missing_entry_or_atr"
            else:
                atr14 = prior_close * atr_pct
                try:
                    exits = make_exits(
                        entry_px,
                        atr14,
                        trade_date=trade_date,
                        is_half_day=is_half_day,
                        cfg=cfg,
                    )
                    barrier = triple_barrier(
                        symbol_bars,
                        entry_ts=entry_ts,
                        entry_px=entry_px,
                        tp_px=exits.tp_px,
                        sl_px=exits.sl_px,
                        time_stop=min(exits.time_stop_utc, session_close_utc),
                    )
                    outcome_label = barrier.which
                    exit_ts_utc = barrier.exit_ts
                    exit_px = barrier.exit_px
                    gross_return = barrier.ret
                    outcome_provenance = barrier.provenance
                except ValueError as exc:
                    outcome_label = "unavailable"
                    outcome_status = "incomplete_minute_path"
                    outcome_detail = str(exc)

        rows.append(
            {
                "symbol": symbol,
                "session_date": trade_date,
                "selection_rank": int(candidate["selection_rank"]),
                "rvol": _number(candidate.get("rvol")),
                "prior_close": prior_close,
                "adv_usd": _number(candidate.get("adv_usd")),
                "market_cap": _number(candidate.get("market_cap")),
                "tier": _text(candidate.get("tier")),
                "beta": _number(candidate.get("beta")),
                "atr_pct": _number(candidate.get("atr_pct")),
                "event_count": int(candidate.get("event_count") or 0),
                "catalyst_categories": _list_of_text(
                    candidate.get("catalyst_categories")
                ),
                "evidence_event_ids": _list_of_text(candidate.get("evidence_event_ids")),
                "evidence_sources": _list_of_text(candidate.get("evidence_sources")),
                "model_score": _number(score.get("raw_probability")),
                "model_score_status": _text(score.get("calibration_status"))
                or "unavailable",
                "model_score_approved": bool(score.get("approved_for_kernel", False)),
                "model_id": _text(score.get("model_id")),
                "model_prompt_sha256": _text(score.get("prompt_sha256")),
                "signal_triggered": triggered,
                "signal_reason": str(signal["reason"]),
                "opening_range_high": _number(signal.get("opening_range_high")),
                "opening_range_low": _number(signal.get("opening_range_low")),
                "opening_range_open": _number(signal.get("opening_range_open")),
                "opening_range_close": _number(signal.get("opening_range_close")),
                "trigger_ts_utc": signal.get("trigger_ts_utc"),
                "entry_ts_utc": signal.get("entry_ts_utc"),
                "entry_px": _number(signal.get("entry_px")),
                "outcome_label": outcome_label,
                "outcome_status": outcome_status,
                "outcome_detail": outcome_detail,
                "exit_ts_utc": exit_ts_utc,
                "exit_px": exit_px,
                "gross_return": gross_return,
                "net_return": None,
                "net_return_status": "unavailable_missing_quote_spread",
                "rth_open": rth_open,
                "rth_high": rth_high,
                "rth_low": rth_low,
                "rth_close": rth_close,
                "rth_high_return": rth_high_return,
                "rth_close_return": rth_close_return,
                "signal_provenance": str(signal["provenance"]),
                "outcome_provenance": outcome_provenance,
                "episode_provenance": (
                    f"research.postmortem:{EPISODE_SCHEMA_VERSION}@{trade_date.isoformat()}|"
                    "facts=frozen_selection+full_rth|net_costs=unavailable"
                ),
            }
        )
    return pl.DataFrame(rows).sort("selection_rank", "symbol")
