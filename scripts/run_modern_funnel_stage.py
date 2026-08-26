"""Execute one durable stage of the modern three-wave Paper funnel."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
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
from operations.autonomous_selection_handoff import (
    create_open_confirmation,
    load_open_confirmation,
)
from operations.feishu_base import FeishuBaseEventClient, InvestmentTable
from operations.livermore_push import LivermorePushClient, configured_identity
from operations.local_env import load_project_env, project_data_root
from operations.paper_state import PaperStateStore
from schedule.modern_funnel import FunnelStage
from scripts.monitor_modern_momentum_forward import SOURCE, _latest_pool

ROOT = Path(__file__).resolve().parents[1]
EASTERN = ZoneInfo("America/New_York")
MAX_SPREAD = 0.001
MIN_PREMARKET_DOLLAR_VOLUME = 1_000_000.0
MIN_OPEN_DOLLAR_VOLUME = 1_000_000.0
STRATEGY_VERSION = "modern-h15.v1"
CATALYST_LABELS = {
    "earnings": "财报",
    "contract_partnership": "合同",
    "financing_dilution": "融资",
    "other_material": "重大事项",
    "management_change": "管理层变动",
    "general_news": "行业消息",
}
CRYPTO_NEWS_SYMBOLS = {"COIN", "MSTR"}


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


def _candidate_reason(row: dict[str, object]) -> str:
    reasons = _reasons(row)
    if reasons:
        return "、".join(reasons)
    categories = row.get("catalyst_categories")
    catalyst = (
        "/".join(str(item) for item in categories)
        if isinstance(categories, (list, tuple)) and categories
        else "催化N/A"
    )
    return (
        f"催化={catalyst}、RVOL={row.get('rvol', 'N/A')}、"
        f"盘前涨幅={row.get('premarket_return', 'N/A')}、"
        f"市值={row.get('forward_market_cap', 'N/A')}"
    )


def _first_wave_catalyst(row: dict[str, object]) -> str:
    categories = row.get("catalyst_categories")
    if not isinstance(categories, (list, tuple)) or not categories:
        raise ValueError("first-wave catalyst category is required")
    if str(row.get("symbol", "")) in CRYPTO_NEWS_SYMBOLS and "general_news" in categories:
        return "加密行业消息"
    labels = tuple(dict.fromkeys(CATALYST_LABELS.get(str(item), "重大事项") for item in categories))
    return "/".join(labels)


def _first_wave_number(row: dict[str, object], field: str) -> float:
    try:
        value = float(str(row[field]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"first-wave {field} is required") from exc
    if not math.isfinite(value):
        raise ValueError(f"first-wave {field} must be finite")
    return value


def _first_wave_message(candidates: list[dict[str, object]]) -> str:
    lines = ["第一波观察池：", ""]
    for index, row in enumerate(candidates, start=1):
        symbol = str(row["symbol"])
        rvol = _first_wave_number(row, "rvol")
        premarket_return = _first_wave_number(row, "premarket_return")
        lines.append(
            f"{index}. {symbol}：{_first_wave_catalyst(row)}，"
            f"RVOL {rvol:.2f}，盘前 {premarket_return:+.2%}"
        )
    return "\n".join(lines)


def _open_plan_lines(candidates: list[dict[str, object]]) -> tuple[str, ...]:
    lines: list[str] = []
    for row in candidates:
        symbol = str(row["symbol"])
        lines.extend(
            (
                f"{symbol} 预案：09:56 ET后，完整5分钟K突破H15且位于上升VWAP上方，"
                "MACD为正并增强、成交量确认后才允许入场；",
                "参考入场：触发时最新SIP卖价，实际点差与滑点合计不得超过0.10%；",
                "失效/止损：跌回H15、失守VWAP或更高低点；含滑点全包止损不超过2%；",
                "止盈/退出：3R目标或趋势退出；15:00后禁止新仓，15:50前全部清仓；",
                "仓位：按账户风险动态反算，单票0.5%、板块0.75%、组合1.5%，"
                "首次/二次尝试60%/40%。",
            )
        )
    return tuple(lines)


def _execution_summary(stage: FunnelStage, row: dict[str, object]) -> str:
    if stage is FunnelStage.OPEN_CONFIRMATION:
        return "".join(_open_plan_lines([row]))
    return f"阶段={stage.value}；只记录状态转换，不记录轮询行情"


def _selection_event_fields(
    *,
    trade_date: date,
    stage: FunnelStage,
    row: dict[str, object],
    kept: bool,
    observed_at_utc: datetime,
) -> dict[str, object]:
    symbol = str(row["symbol"])
    event_id = f"funnel:{trade_date.isoformat()}:{stage.value}:{symbol}"
    if not kept:
        next_action = "已剔除；纳入无成交复盘"
        execution_summary = f"剔除理由={_candidate_reason(row)}"
    else:
        next_action = (
            "等待下一层漏斗"
            if stage is not FunnelStage.OPEN_CONFIRMATION
            else "等待H15入场条件"
        )
        execution_summary = _execution_summary(stage, row)
    return {
        "运行ID": event_id,
        "选股时间": observed_at_utc,
        "股票名称": symbol,
        "股票代码": symbol,
        "市场": "美股",
        "信号类型": "综合",
        "模拟动作": "候选" if kept else "不操作",
        "状态": "新信号" if kept else "已失效",
        "触发理由": _candidate_reason(row),
        "下一动作": next_action,
        "执行摘要": execution_summary,
        "策略版本": STRATEGY_VERSION,
        "数据源状态": "alpaca.sip|production=false",
    }


def _stage_observed_at(trade_date: date, stage: FunnelStage) -> datetime:
    stage_time = {
        FunnelStage.FIRST_WAVE: time(8),
        FunnelStage.SECOND_WAVE: time(9, 25),
        FunnelStage.OPEN_CONFIRMATION: time(9, 35),
    }[stage]
    return datetime.combine(trade_date, stage_time, EASTERN).astimezone(UTC)


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
    now = _stage_observed_at(trade_date, stage)
    stage_rows = tuple((row, True) for row in candidates) + tuple(
        (row, False) for row in rejected
    )
    record_ids = tuple(
        base.record_event(
            InvestmentTable.SELECTION,
            f"funnel:{trade_date.isoformat()}:{stage.value}:{row['symbol']}",
            _selection_event_fields(
                trade_date=trade_date,
                stage=stage,
                row=row,
                kept=kept,
                observed_at_utc=now,
            ),
        )
        for row, kept in stage_rows
    )
    stage_title = {
        FunnelStage.FIRST_WAVE: "第一波观察池",
        FunnelStage.SECOND_WAVE: "第二波盘前复核",
        FunnelStage.OPEN_CONFIRMATION: "第三波开盘确认",
    }[stage]
    kept_text = "；".join(
        f"{row['symbol']}：{_candidate_reason(row)}" for row in candidates
    ) or "无"
    rejected_text = "；".join(
        f"{row['symbol']}：{'、'.join(_reasons(row))}"
        for row in rejected
    ) or "无"
    plan_text = (
        "\n".join(_open_plan_lines(candidates)) + "\n"
        if stage is FunnelStage.OPEN_CONFIRMATION
        else ""
    )
    body = (
        _first_wave_message(candidates)
        if stage is FunnelStage.FIRST_WAVE
        else (
            f"【AI量化漏斗｜{stage_title}】\n"
            f"交易日：{trade_date.isoformat()}\n"
            f"保留：{kept_text}\n"
            f"剔除：{rejected_text}\n"
            f"{plan_text}"
            "仅Alpaca Paper模拟盘；本消息不代表已经成交。"
        )
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
    path = day_root / "first_wave_pool.json"
    if path.exists():
        existing = _read_json(path)
        raw_rows = existing.get("candidates")
        if not isinstance(raw_rows, list):
            raise ValueError("first-wave retry artifact is invalid")
        rows = raw_rows
    else:
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
    path = day_root / "second_wave_pool.json"
    if path.exists():
        existing = _read_json(path)
        kept = existing.get("candidates")
        rejected = existing.get("rejected")
        if not isinstance(kept, list) or not isinstance(rejected, list):
            raise ValueError("second-wave retry artifact is invalid")
    else:
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
    store = PaperStateStore(run_dir / "paper-state.sqlite3")
    active_owner = store.active_run_owner(trade_date, observed_at_utc=datetime.now(UTC))
    if active_owner is not None:
        prefix = "pid-"
        if not active_owner.startswith(prefix) or not active_owner.removeprefix(prefix).isdigit():
            raise RuntimeError("active Paper monitor lease has an invalid owner")
        return int(active_owner.removeprefix(prefix))
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
    confirmation_path = day_root / "open_confirmation.json"
    if confirmation_path.exists():
        confirmation = load_open_confirmation(confirmation_path)
        authorization = confirmation.authorization
        if (
            authorization.trade_date != args.trade_date
            or authorization.strategy_version != STRATEGY_VERSION
        ):
            raise RuntimeError("existing open confirmation does not match this stage")
        paper_pid = _launch_paper_if_confirmed(args.trade_date, confirmation_path)
        record_ids = tuple(authorization.feishu_record_id.split(","))
        return {
            **_receipt(
                confirmation_path,
                record_ids,
                authorization.livermore_message_id,
            ),
            "authorization_id": authorization.open_confirmation_id,
            "symbols": ",".join(authorization.candidate_pool),
            "paper_started": str(paper_pid is not None).lower(),
            "paper_pid": str(paper_pid or ""),
        }

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
    decision_path = day_root / "open_decision.json"
    if decision_path.exists():
        decision = _read_json(decision_path)
        kept = decision.get("candidates")
        rejected = decision.get("rejected")
        if not isinstance(kept, list) or not isinstance(rejected, list):
            raise ValueError("open-decision retry artifact is invalid")
    else:
        symbols = _symbols(source)
        session = _session(args)
        open_utc = session["market_open_utc"]
        bars = fetch_bars(symbols, open_utc, open_utc + timedelta(minutes=5), feed="sip")
        kept, rejected = evaluate_open_confirmation(candidates, bars)
        _write_json(
            decision_path,
            {
                "schema_version": "modern_funnel.open_decision.v1",
                "trade_date": args.trade_date.isoformat(),
                "generated_at_utc": datetime.now(UTC),
                "candidates": kept,
                "rejected": rejected,
            },
        )
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
        confirmation_path=confirmation_path,
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
        confirmation_path,
    )
    return {
        **_receipt(confirmation_path, record_ids, message_id),
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
