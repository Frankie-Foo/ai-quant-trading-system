"""Run today's modern H15 momentum strategy as a no-order forward shadow."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
from pydantic import SecretStr

from data_plane.calendar import build_xnys_schedule
from data_plane.providers.alpaca import fetch_bars
from operations.livermore_push import LivermorePushClient, configured_identity
from operations.local_env import load_project_env, project_data_root
from research.modern_momentum import ModernMomentumConfig, evaluate_modern_momentum
from scripts.run_modern_momentum_backtest import _entry_spread, _quote_spreads

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "research.modern_momentum.forward_pool"
RISK_USD = 1_000.0


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
    start_at = opened + timedelta(minutes=26)
    stop_at = opened + timedelta(minutes=377)
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
        "positions": {},
        "events": [],
        "message_ids": [],
        "orders_enabled": False,
        "status": "waiting",
    }
    config = ModernMomentumConfig()
    prior_closes = {
        str(row["symbol"]): float(row["price"])
        for row in pool.iter_rows(named=True)
        if isinstance(row["price"], (int, float))
    }
    market_caps = {
        str(row["symbol"]): float(row["forward_market_cap"]) for row in pool.iter_rows(named=True)
    }
    rvols = {str(row["symbol"]): float(row["rvol"]) for row in pool.iter_rows(named=True)}
    positions: dict[str, dict[str, object]] = {}
    completed: set[str] = set()
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
            for symbol in symbols:
                if symbol in completed:
                    continue
                symbol_bars = bars.filter(pl.col("symbol") == symbol)
                if symbol_bars.is_empty() or symbol not in prior_closes:
                    continue
                preliminary = evaluate_modern_momentum(
                    symbol_bars,
                    session_open_utc=opened,
                    prior_close=prior_closes[symbol],
                    market_cap=market_caps[symbol],
                    premarket_rvol=rvols[symbol],
                    config=config,
                )
                if preliminary is None:
                    continue
                spread = _entry_spread(symbol, preliminary)
                trade = evaluate_modern_momentum(
                    symbol_bars,
                    session_open_utc=opened,
                    prior_close=prior_closes[symbol],
                    market_cap=market_caps[symbol],
                    premarket_rvol=rvols[symbol],
                    config=config,
                    relative_spread=spread,
                )
                if trade is None:
                    continue
                if symbol not in positions:
                    shares = int(RISK_USD / (trade.entry_px * trade.all_in_stop_pct))
                    positions[symbol] = {
                        "entry_ts_utc": trade.entry_ts_utc,
                        "entry_px": trade.entry_px,
                        "shares": shares,
                        "stop_level": trade.stop_level,
                        "target_level": trade.target_level,
                    }
                    event = {
                        "type": "shadow_buy",
                        "symbol": symbol,
                        "ts_utc": trade.entry_ts_utc,
                        "price": trade.entry_px,
                        "shares": shares,
                    }
                    events.append(event)
                    body = (
                        f"【现代H15动量｜影子买入】{symbol}\n"
                        f"模拟价：${trade.entry_px:.2f}；股数：{shares}；"
                        f"止损：${trade.stop_level:.2f}；3R目标：${trade.target_level:.2f}。\n"
                        "仅前向影子模拟，未提交Alpaca订单。"
                    )
                    message_ids.append(client.push(body))
                if trade.exit_reason == "data_end":
                    continue
                position = positions[symbol]
                position_shares = position["shares"]
                position_entry = position["entry_px"]
                if not isinstance(position_shares, int) or not isinstance(
                    position_entry, (int, float)
                ):
                    raise ValueError("shadow position state is invalid")
                _, exit_spread, _ = _quote_spreads(symbol, trade)
                raw_exit = trade.exit_px / (1 - spread / 2 - config.market_impact_pct)
                exit_px = raw_exit * (1 - exit_spread / 2 - config.market_impact_pct)
                pnl = position_shares * (exit_px - float(position_entry))
                event = {
                    "type": "shadow_sell",
                    "symbol": symbol,
                    "ts_utc": trade.exit_ts_utc,
                    "price": exit_px,
                    "pnl": pnl,
                    "reason": trade.exit_reason,
                }
                events.append(event)
                body = (
                    f"【现代H15动量｜影子卖出】{symbol}\n"
                    f"模拟价：${exit_px:.2f}；盈亏：${pnl:,.2f}；原因：{trade.exit_reason}。\n"
                    "仅前向影子模拟，未提交Alpaca订单。"
                )
                message_ids.append(client.push(body))
                positions.pop(symbol)
                completed.add(symbol)
            state.update(
                {
                    "positions": positions,
                    "completed_symbols": sorted(completed),
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
