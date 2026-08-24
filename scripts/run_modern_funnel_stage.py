"""Execute one durable stage of the modern three-wave Paper funnel."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time as time_module
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl
from pydantic import SecretStr

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.providers.alpaca import fetch_bars, fetch_quotes
from data_plane.storage import persist_snapshot
from operations.autonomous_notifications import AutonomousNotificationLedger
from operations.autonomous_selection_handoff import create_open_confirmation
from operations.feishu_base import FeishuBaseEventClient, InvestmentTable
from operations.livermore_push import LivermorePushClient, configured_identity
from operations.local_env import load_project_env, project_data_root
from schedule.modern_funnel import FunnelStage
from scripts.monitor_modern_momentum_forward import SOURCE, _latest_pool

ROOT = Path(__file__).resolve().parents[1]
EASTERN = ZoneInfo("America/New_York")
MAX_SPREAD = 0.001
MIN_PREMARKET_DOLLAR_VOLUME = 1_000_000.0
MIN_OPEN_DOLLAR_VOLUME = 1_000_000.0
STRATEGY_VERSION = "modern-h15.v1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, type=FunnelStage)
    parser.add_argument("--trade-date", required=True, type=date.fromisoformat)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    parser.add_argument("--state-root", type=Path, default=ROOT / "runs" / "autonomous")
    return parser


def _run_module(module: str, trade_date: date, data_root: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            module,
            "--trade-date",
            trade_date.isoformat(),
            "--data-root",
            str(data_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{module} failed with exit code {completed.returncode}")


def _write_json(path: Path, payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if json.loads(existing) != json.loads(encoded):
            raise RuntimeError(f"immutable funnel artifact changed: {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"funnel artifact is not an object: {path.name}")
    return value


def _symbols(payload: dict[str, Any]) -> tuple[str, ...]:
    rows = payload.get("candidates")
    if not isinstance(rows, list):
        raise ValueError("funnel candidates are missing")
    symbols = tuple(
        str(row["symbol"]).strip().upper()
        for row in rows
        if isinstance(row, dict) and str(row.get("symbol", "")).strip()
    )
    if len(symbols) != len(rows) or len(symbols) != len(set(symbols)):
        raise ValueError("funnel candidate identities are invalid")
    return symbols


def _latest_rows(frame: pl.DataFrame, symbols: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    return {
        symbol: frame.filter(pl.col("symbol") == symbol).sort("ts_utc").tail(1).row(
            0, named=True
        )
        for symbol in symbols
        if not frame.filter(pl.col("symbol") == symbol).is_empty()
    }


def _number(value: object) -> float:
    if value is None:
        return 0.0
    return float(str(value))


def _reasons(row: dict[str, object]) -> tuple[str, ...]:
    value = row.get("reasons", ())
    if not isinstance(value, (list, tuple)):
        raise ValueError("funnel reasons must be a sequence")
    return tuple(str(item) for item in value)


def evaluate_second_wave(
    candidates: list[dict[str, Any]],
    bars: pl.DataFrame,
    quotes: pl.DataFrame,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Apply observable 09:25 liquidity and acceptance gates to the frozen pool."""

    symbols = tuple(str(row["symbol"]) for row in candidates)
    last_bars = _latest_rows(bars, symbols)
    last_quotes = _latest_rows(quotes, symbols)
    kept: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    for candidate in candidates:
        symbol = str(candidate["symbol"])
        symbol_bars = bars.filter(pl.col("symbol") == symbol)
        bar = last_bars.get(symbol)
        quote = last_quotes.get(symbol)
        reasons: list[str] = []
        if bar is None or quote is None or symbol_bars.is_empty():
            reasons.append("Alpaca SIP盘前行情不完整")
        else:
            bid = float(quote["bid_price"])
            ask = float(quote["ask_price"])
            midpoint = (bid + ask) / 2
            spread = (ask - bid) / midpoint if midpoint > 0 else float("inf")
            volume = float(symbol_bars["volume"].sum())
            dollar_volume = float(
                (symbol_bars["close"] * symbol_bars["volume"]).sum()
            )
            vwap = dollar_volume / volume if volume > 0 else 0.0
            close = float(bar["close"])
            if spread > MAX_SPREAD:
                reasons.append(f"点差{spread:.2%}超过0.10%")
            if dollar_volume < MIN_PREMARKET_DOLLAR_VOLUME:
                reasons.append("盘前成交额不足100万美元")
            if close <= vwap:
                reasons.append("最新价未站上盘前VWAP")
            candidate = {
                **candidate,
                "observed_price": close,
                "observed_vwap": vwap,
                "observed_spread": spread,
                "observed_dollar_volume": dollar_volume,
            }
        result = {**candidate, "reasons": reasons or ["量价、VWAP、点差和流动性通过"]}
        (rejected if reasons else kept).append(result)
    kept.sort(
        key=lambda row: (
            _number(row.get("observed_dollar_volume", 0)),
            _number(row.get("premarket_return", 0)),
        ),
        reverse=True,
    )
    return kept[:6], rejected + kept[6:]


def evaluate_open_confirmation(
    candidates: list[dict[str, Any]],
    bars: pl.DataFrame,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Use only completed 09:30-09:35 minute bars to authorize later H15 monitoring."""

    kept: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    for candidate in candidates:
        symbol = str(candidate["symbol"])
        symbol_bars = bars.filter(pl.col("symbol") == symbol).sort("ts_utc")
        reasons: list[str] = []
        if symbol_bars.height < 5:
            reasons.append("开盘前5分钟完整K线不足")
            result = {**candidate, "reasons": reasons}
            rejected.append(result)
            continue
        window = symbol_bars.head(5)
        first_open = float(window["open"][0])
        close = float(window["close"][-1])
        high = _number(window["high"].max())
        low = _number(window["low"].min())
        volume = float(window["volume"].sum())
        dollar_volume = float((window["close"] * window["volume"]).sum())
        vwap = dollar_volume / volume if volume > 0 else 0.0
        return_5m = close / first_open - 1 if first_open > 0 else float("-inf")
        close_location = (close - low) / (high - low) if high > low else 0.0
        if close <= vwap:
            reasons.append("开盘5分钟收盘未站上VWAP")
        if return_5m <= 0:
            reasons.append("开盘5分钟未形成正收益")
        if close_location < 0.6:
            reasons.append("收盘位于5分钟区间下部")
        if dollar_volume < MIN_OPEN_DOLLAR_VOLUME:
            reasons.append("开盘5分钟成交额不足100万美元")
        result = {
            **candidate,
            "open_price": first_open,
            "open_5m_close": close,
            "open_5m_high": high,
            "open_5m_low": low,
            "open_5m_vwap": vwap,
            "open_5m_return": return_5m,
            "open_5m_dollar_volume": dollar_volume,
            "reasons": reasons or ["开盘5分钟量价与市场接受度通过"],
        }
        (rejected if reasons else kept).append(result)
    kept.sort(
        key=lambda row: (
            _number(row.get("open_5m_return", 0)),
            _number(row.get("open_5m_dollar_volume", 0)),
        ),
        reverse=True,
    )
    return kept[:3], rejected + kept[3:]


def _push_client() -> LivermorePushClient:
    app_id, channel_id = configured_identity(os.environ)
    return LivermorePushClient(
        app_id=app_id,
        app_secret=SecretStr(os.getenv("VPS_LIVERMORE_APP_SECRET", "")),
        channel_id=channel_id,
    )


def _message_id(ledger: AutonomousNotificationLedger, key: str) -> str | None:
    for record in ledger.list_records():
        if record["notification_key"] == key:
            return str(record["message_id"])
    return None


def _push_once(
    ledger: AutonomousNotificationLedger,
    push: LivermorePushClient,
    *,
    key: str,
    body: str,
    payload: dict[str, object],
) -> str:
    now = datetime.now(UTC)
    claim = ledger.claim(key, claimed_at_utc=now)
    if claim == "sent":
        existing = _message_id(ledger, key)
        if existing is None:
            raise RuntimeError("Livermore receipt is missing")
        return existing
    if claim != "claimed":
        raise RuntimeError("Livermore notification is already in flight")
    try:
        message_id = push.push(body)
    except Exception:
        ledger.release_claim(key)
        raise
    ledger.record(
        key,
        message_id=message_id,
        sent_at_utc=now,
        message_body=body,
        payload=payload,
    )
    return message_id


def _publish_stage(
    *,
    trade_date: date,
    stage: FunnelStage,
    candidates: list[dict[str, object]],
    rejected: list[dict[str, object]],
    state_root: Path,
) -> tuple[tuple[str, ...], str]:
    base = FeishuBaseEventClient.from_environment(os.environ)
    if base is None:
        raise RuntimeError("dedicated investment Feishu Base is required")
    now = datetime.now(UTC)
    record_ids = tuple(
        base.record_event(
            InvestmentTable.SELECTION,
            f"funnel:{trade_date.isoformat()}:{stage.value}:{row['symbol']}",
            {
                "运行ID": f"funnel:{trade_date.isoformat()}:{stage.value}:{row['symbol']}",
                "选股时间": now,
                "股票名称": str(row["symbol"]),
                "股票代码": str(row["symbol"]),
                "市场": "美股",
                "信号类型": "综合",
                "模拟动作": "候选",
                "状态": "新信号",
                "触发理由": "；".join(_reasons(row)),
                "下一动作": (
                    "等待下一层漏斗"
                    if stage is not FunnelStage.OPEN_CONFIRMATION
                    else "等待H15入场条件"
                ),
                "执行摘要": f"阶段={stage.value}；只记录状态转换，不记录轮询行情",
                "策略版本": STRATEGY_VERSION,
                "数据源状态": "alpaca.sip|production=false",
            },
        )
        for row in candidates
    )
    kept_text = "、".join(str(row["symbol"]) for row in candidates) or "无"
    rejected_text = "；".join(
        f"{row['symbol']}：{'、'.join(_reasons(row))}"
        for row in rejected
    ) or "无"
    body = (
        f"【AI量化漏斗｜{stage.value}】\n"
        f"交易日：{trade_date.isoformat()}\n"
        f"保留：{kept_text}\n"
        f"剔除：{rejected_text}\n"
        "仅Alpaca Paper模拟盘；本消息不代表已经成交。"
    )
    ledger = AutonomousNotificationLedger(
        state_root / trade_date.isoformat() / "funnel-notifications.sqlite3"
    )
    push = _push_client()
    try:
        message_id = _push_once(
            ledger,
            push,
            key=f"funnel:{trade_date.isoformat()}:{stage.value}",
            body=body,
            payload={"kept": kept_text, "rejected": rejected_text},
        )
    finally:
        push.close()
    return record_ids, message_id


def _first_wave(args: argparse.Namespace, day_root: Path) -> dict[str, object]:
    _run_module("schedule.premarket", args.trade_date, args.data_root)
    _run_module("scripts.prepare_modern_momentum_forward", args.trade_date, args.data_root)
    pool = _latest_pool(args.data_root, args.trade_date)
    rows = pool.to_dicts()
    payload: dict[str, object] = {
        "schema_version": "modern_funnel.first_wave.v1",
        "trade_date": args.trade_date.isoformat(),
        "generated_at_utc": datetime.now(UTC),
        "candidates": rows,
    }
    path = day_root / "first_wave_pool.json"
    _write_json(path, payload)
    record_ids, message_id = _publish_stage(
        trade_date=args.trade_date,
        stage=FunnelStage.FIRST_WAVE,
        candidates=rows,
        rejected=[],
        state_root=args.state_root,
    )
    return _receipt(path, record_ids, message_id)


def _session(args: argparse.Namespace) -> dict[str, Any]:
    schedule = build_xnys_schedule(args.trade_date, args.trade_date)
    if schedule.height != 1:
        raise RuntimeError("trade date is not an XNYS session")
    return schedule.row(0, named=True)


def _second_wave(args: argparse.Namespace, day_root: Path) -> dict[str, object]:
    source = _read_json(day_root / "first_wave_pool.json")
    candidates = source.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("first-wave candidates are missing")
    symbols = _symbols(source)
    session = _session(args)
    open_utc = session["market_open_utc"]
    now = datetime.now(UTC)
    premarket_start = datetime.combine(args.trade_date, time(4), EASTERN).astimezone(UTC)
    bars = fetch_bars(symbols, premarket_start, min(now, open_utc), feed="sip")
    quotes = fetch_quotes(symbols, now - timedelta(seconds=15), now, feed="sip")
    kept, rejected = evaluate_second_wave(candidates, bars, quotes)
    payload: dict[str, object] = {
        "schema_version": "modern_funnel.second_wave.v1",
        "trade_date": args.trade_date.isoformat(),
        "generated_at_utc": now,
        "candidates": kept,
        "rejected": rejected,
    }
    path = day_root / "second_wave_pool.json"
    _write_json(path, payload)
    record_ids, message_id = _publish_stage(
        trade_date=args.trade_date,
        stage=FunnelStage.SECOND_WAVE,
        candidates=kept,
        rejected=rejected,
        state_root=args.state_root,
    )
    return _receipt(path, record_ids, message_id)


def _latest_pool_snapshot(data_root: Path, trade_date: date) -> tuple[Path, DatasetSnapshot]:
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(f"{SOURCE}-*/data.parquet"):
        frame = pl.read_parquet(path, columns=["session_date"])
        if frame["session_date"].unique().to_list() != [trade_date]:
            continue
        snapshot = DatasetSnapshot.model_validate_json(
            (path.parent / "manifest.json").read_text(encoding="utf-8")
        ).assert_usable()
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        raise FileNotFoundError("modern momentum pool snapshot is missing")
    _, path, snapshot = max(matches, key=lambda item: item[0])
    return path, snapshot


def _freeze_final_pool(
    data_root: Path,
    trade_date: date,
    symbols: tuple[str, ...],
) -> DatasetSnapshot:
    source_path, parent = _latest_pool_snapshot(data_root, trade_date)
    source_pool = pl.read_parquet(source_path)
    final_pool = source_pool.filter(pl.col("symbol").is_in(symbols)).sort("forward_rank")
    if final_pool.height != len(symbols):
        raise RuntimeError("final pool diverged from its frozen source")
    snapshot, _ = persist_snapshot(
        final_pool,
        root=data_root,
        source=SOURCE,
        schema_version="modern_momentum_forward_pool.v1",
        checks=(
            DataQualityCheck(
                name="open_confirmation_pool",
                severity=QualitySeverity.CRITICAL,
                passed=0 < final_pool.height <= 3,
                observed=str(final_pool.height),
                expected="1..3",
                provenance="scripts.run_modern_funnel_stage.v1",
            ),
        ),
        parent_snapshot_ids=(parent.dataset_id,),
    )
    return snapshot.assert_usable()


def _plan_payload(trade_date: date, rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": "modern_h15_paper_plan.v1",
        "trade_date": trade_date.isoformat(),
        "strategy_version": STRATEGY_VERSION,
        "paper_only": True,
        "entry_after_et": "09:56",
        "new_entry_cutoff_et": "15:00",
        "cancel_unfilled_et": "15:45",
        "flatten_et": "15:50",
        "maximum_all_in_stop_pct": 0.02,
        "target_r": 3,
        "candidates": rows,
    }


def _launch_paper_if_confirmed(trade_date: date, confirmation_path: Path) -> int | None:
    if os.getenv("AI_QUANT_PAPER_RUNTIME_CONFIRMED", "").strip().lower() != "true":
        return None
    smoke_raw = os.getenv("AI_QUANT_PAPER_SMOKE_MAX_NOTIONAL", "").strip()
    if not smoke_raw or not 0 < float(smoke_raw) <= 100:
        raise RuntimeError("confirmed Paper startup requires a smoke cap no greater than $100")
    run_dir = ROOT / "runs" / "modern-momentum" / trade_date.isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "scripts.monitor_modern_momentum_paper",
        "--trade-date",
        trade_date.isoformat(),
        "--confirmation-path",
        str(confirmation_path),
        "--arm-paper",
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with (
        (run_dir / "paper-monitor.out.log").open("ab") as stdout,
        (run_dir / "paper-monitor.err.log").open("ab") as stderr,
    ):
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
        )
    time_module.sleep(2)
    return_code = process.poll()
    if return_code is not None:
        raise RuntimeError(f"Paper monitor failed during startup with exit code {return_code}")
    return process.pid


def _open_confirmation(args: argparse.Namespace, day_root: Path) -> dict[str, object]:
    source = _read_json(day_root / "second_wave_pool.json")
    candidates = source.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("second-wave candidates are missing")
    if not candidates:
        prior_rejected = source.get("rejected", [])
        if not isinstance(prior_rejected, list):
            raise ValueError("second-wave rejected candidates are invalid")
        record_ids, message_id = _publish_stage(
            trade_date=args.trade_date,
            stage=FunnelStage.OPEN_CONFIRMATION,
            candidates=[],
            rejected=prior_rejected,
            state_root=args.state_root,
        )
        path = day_root / "open_no_trade.json"
        _write_json(
            path,
            {
                "schema_version": "modern_funnel.no_trade.v1",
                "trade_date": args.trade_date.isoformat(),
                "reason": "第二波无候选",
            },
        )
        return _receipt(path, record_ids, message_id)
    symbols = _symbols(source)
    session = _session(args)
    open_utc = session["market_open_utc"]
    bars = fetch_bars(symbols, open_utc, open_utc + timedelta(minutes=5), feed="sip")
    kept, rejected = evaluate_open_confirmation(candidates, bars)
    record_ids, message_id = _publish_stage(
        trade_date=args.trade_date,
        stage=FunnelStage.OPEN_CONFIRMATION,
        candidates=kept,
        rejected=rejected,
        state_root=args.state_root,
    )
    if not kept:
        path = day_root / "open_no_trade.json"
        _write_json(
            path,
            {
                "schema_version": "modern_funnel.no_trade.v1",
                "trade_date": args.trade_date.isoformat(),
                "reason": "开盘确认无标的通过",
                "rejected": rejected,
            },
        )
        return _receipt(path, record_ids, message_id)
    final_symbols = tuple(str(row["symbol"]) for row in kept)
    snapshot = _freeze_final_pool(args.data_root, args.trade_date, final_symbols)
    plan_path = day_root / "modern_h15_paper_plan.json"
    _write_json(plan_path, _plan_payload(args.trade_date, kept))
    authorization = create_open_confirmation(
        confirmation_path=day_root / "open_confirmation.json",
        config_path=plan_path,
        trade_date=args.trade_date,
        selection_snapshot_id=snapshot.dataset_id,
        candidate_pool=final_symbols,
        feishu_record_ids=record_ids,
        livermore_message_id=message_id,
        strategy_version=STRATEGY_VERSION,
        generated_at_utc=datetime.now(UTC),
    )
    paper_pid = _launch_paper_if_confirmed(
        args.trade_date,
        day_root / "open_confirmation.json",
    )
    return {
        **_receipt(day_root / "open_confirmation.json", record_ids, message_id),
        "authorization_id": authorization.open_confirmation_id,
        "symbols": ",".join(final_symbols),
        "paper_started": str(paper_pid is not None).lower(),
        "paper_pid": str(paper_pid or ""),
    }


def _receipt(path: Path, record_ids: tuple[str, ...], message_id: str) -> dict[str, object]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "ok": True,
        "receipt_id": digest,
        "artifact_path": str(path),
        "feishu_record_ids": ",".join(record_ids),
        "livermore_message_id": message_id,
    }


def main() -> int:
    load_project_env(ROOT)
    args = _parser().parse_args()
    day_root = args.state_root / args.trade_date.isoformat()
    if args.stage is FunnelStage.FIRST_WAVE:
        receipt = _first_wave(args, day_root)
    elif args.stage is FunnelStage.SECOND_WAVE:
        receipt = _second_wave(args, day_root)
    else:
        receipt = _open_confirmation(args, day_root)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
