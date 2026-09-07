"""Observe today's current modern H15 signals without simulating fills or placing orders."""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
from pydantic import SecretStr

from data_plane.calendar import build_xnys_schedule
from data_plane.providers.alpaca import fetch_bars, fetch_quotes
from kernel.quote_costs import latest_nbbo_spread
from operations.livermore_push import LivermorePushClient, configured_identity
from operations.local_env import load_project_env, project_data_root
from research.modern_momentum import (
    ModernMomentumConfig,
    latest_modern_momentum_signal,
    modern_strategy_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "research.modern_momentum.forward_pool"


def _latest_pool(data_root: Path, trade_date: date) -> pl.DataFrame:
    matches: list[tuple[datetime, Path]] = []
    for path in (data_root / "accepted").glob(f"{SOURCE}-*/data.parquet"):
        frame = pl.read_parquet(path, columns=["session_date"])
        if frame.get_column("session_date").unique().to_list() == [trade_date]:
            matches.append((datetime.fromtimestamp(path.stat().st_mtime, UTC), path))
    if not matches:
        raise FileNotFoundError("modern momentum forward pool is missing")
    return pl.read_parquet(max(matches)[1])


def _push_client() -> LivermorePushClient:
    app_id, channel_id = configured_identity(os.environ)
    return LivermorePushClient(
        app_id=app_id,
        app_secret=SecretStr(os.getenv("VPS_LIVERMORE_APP_SECRET", "")),
        channel_id=channel_id,
    )


def _save(path: Path, state: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    temporary.replace(path)


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True, type=date.fromisoformat)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    data_root = project_data_root(ROOT)
    pool = _latest_pool(data_root, args.trade_date)
    session = build_xnys_schedule(args.trade_date, args.trade_date).row(0, named=True)
    opened = session["market_open_utc"]
    config = ModernMomentumConfig()
    start_at = opened + timedelta(minutes=26)
    stop_at = opened + timedelta(minutes=config.liquidation_minutes)
    symbols = tuple(pool.get_column("symbol").to_list())
    if args.check:
        print(
            json.dumps(
                {
                    "status": "ready",
                    "symbols": symbols,
                    "start_at_utc": start_at,
                    "stop_at_utc": stop_at,
                    "orders_enabled": False,
                    "strategy_manifest": modern_strategy_manifest(config),
                },
                default=str,
            )
        )
        return

    run_dir = ROOT / "runs" / "modern-momentum" / args.trade_date.isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    state: dict[str, object] = {
        "trade_date": args.trade_date.isoformat(),
        "symbols": symbols,
        "mode": "signal_observation_only",
        "strategy_manifest": modern_strategy_manifest(config),
        "events": [],
        "message_ids": [],
        "orders_enabled": False,
        "status": "waiting",
    }
    prior_closes = {
        str(row["symbol"]): float(row["price"])
        for row in pool.iter_rows(named=True)
        if isinstance(row["price"], (int, float))
    }
    market_caps = {
        str(row["symbol"]): float(row["forward_market_cap"]) for row in pool.iter_rows(named=True)
    }
    rvols = {str(row["symbol"]): float(row["rvol"]) for row in pool.iter_rows(named=True)}
    events: list[dict[str, object]] = []
    message_ids: list[str] = []
    last_minute: datetime | None = None
    client = _push_client()
    try:
        while datetime.now(UTC) < stop_at:
            now = datetime.now(UTC)
            if now < start_at:
                _save(state_path, state)
                time.sleep(1)
                continue
            complete_minute = now.replace(second=0, microsecond=0)
            if complete_minute == last_minute:
                time.sleep(1)
                continue
            last_minute = complete_minute
            bars = fetch_bars(symbols, opened, complete_minute)
            quotes = fetch_quotes(
                symbols, now - timedelta(seconds=30), now + timedelta(microseconds=1)
            )
            for symbol in symbols:
                symbol_bars = bars.filter(pl.col("symbol") == symbol)
                if symbol_bars.is_empty() or symbol not in prior_closes:
                    continue
                quote = latest_nbbo_spread(quotes, symbol=symbol, at_utc=now)
                if quote is None:
                    continue
                signal = latest_modern_momentum_signal(
                    symbol_bars,
                    session_open_utc=opened,
                    prior_close=prior_closes[symbol],
                    market_cap=market_caps[symbol],
                    premarket_rvol=rvols[symbol],
                    config=config,
                    asof_utc=now,
                    relative_spread=quote.relative_spread,
                )
                if signal is None:
                    continue
                event = {
                    "type": "shadow_signal",
                    "symbol": symbol,
                    **asdict(signal),
                    "observed_at_utc": now,
                    "quote_provenance": quote.provenance,
                    "entry_relative_spread": quote.relative_spread,
                    "orders_enabled": False,
                }
                events.append(event)
                body = (
                    f"【现代H15动量｜当前信号】{symbol}\n"
                    f"1分钟收盘参考价：${signal.entry_reference:.2f}；"
                    f"止损参考：${signal.stop_level:.2f}；"
                    f"含成本止损比例：{signal.all_in_stop_pct:.2%}。\n"
                    "仅前向信号观察，无成交或盈亏认定，未提交Alpaca订单。"
                )
                message_ids.append(client.push(body))
            state.update(
                {
                    "events": events,
                    "message_ids": message_ids,
                    "status": "running",
                    "last_complete_minute_utc": complete_minute,
                }
            )
            _save(state_path, state)
            time.sleep(1)
        state["status"] = "complete"
        _save(state_path, state)
    finally:
        client.close()


if __name__ == "__main__":
    main()
