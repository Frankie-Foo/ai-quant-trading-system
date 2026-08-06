"""Deterministic post-close opportunity labels for intraday selection review."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import polars as pl

REVIEW_SCHEMA_VERSION = "intraday_selection_postmortem.v1"


@dataclass(frozen=True)
class MoverReason:
    category: str
    detail: str


@dataclass(frozen=True)
class _Attribution:
    reason: MoverReason
    evidence_news_published_utc: datetime | None = None
    evidence_news_headline: str | None = None


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _is_company_specific_news(row: dict[str, Any]) -> bool:
    headline = str(row.get("headline") or "").casefold()
    symbol_count = int(row.get("symbol_count") or 0)
    broad_patterns = (
        "stocks moving",
        "whale alerts",
        "top three",
        "top 3",
        "top 5",
        "top 10",
        "top ten",
        "market update",
        "midday movers",
        "pre-market session",
        "premarket session",
    )
    return symbol_count <= 3 and not any(pattern in headline for pattern in broad_patterns)


def _attribute_mover(
    *,
    gate_row: dict[str, Any] | None,
    news_rows: list[dict[str, Any]],
    selection_cutoff_utc: datetime,
    news_complete: bool,
) -> _Attribution:
    if gate_row is not None and gate_row.get("pass_gate") is True:
        return _Attribution(MoverReason("selected", "已进入盘前最终名单"))
    if gate_row is not None:
        reason = str(gate_row.get("reject_reason") or "unknown_gate")
        translations = {
            "rvol_below_or_equal_min": "盘前RVOL不超过3",
            "missing_rvol": "缺少有效盘前RVOL",
            "premarket_volume_not_confirmed_by_price": "盘前量价方向未确认",
            "missing_rvol;missing_market_cap": "盘前RVOL和市值数据均缺失",
        }
        return _Attribution(
            MoverReason(
                "intentional_gate",
                f"盘前有候选记录但被硬闸淘汰：{translations.get(reason, reason)}",
            )
        )

    material_news = [row for row in news_rows if _is_company_specific_news(row)]
    before_cutoff = sorted(
        (
            row
            for row in material_news
            if isinstance(row.get("published_utc"), datetime)
            and row["published_utc"] <= selection_cutoff_utc
        ),
        key=lambda row: row["published_utc"],
    )
    after_cutoff = sorted(
        (
            row
            for row in material_news
            if isinstance(row.get("published_utc"), datetime)
            and row["published_utc"] > selection_cutoff_utc
        ),
        key=lambda row: row["published_utc"],
    )
    if before_cutoff:
        evidence = before_cutoff[-1]
        headline = str(evidence.get("headline") or "").strip()
        return _Attribution(
            MoverReason(
                "data_or_classifier_gap",
                f"盘前已有公司级新闻但未进入催化池，需检查抓取/分类：{headline[:80]}",
            ),
            evidence_news_published_utc=evidence["published_utc"],
            evidence_news_headline=headline,
        )
    if after_cutoff:
        evidence = after_cutoff[0]
        headline = str(evidence.get("headline") or "").strip()
        return _Attribution(
            MoverReason(
                "late_catalyst",
                f"盘中才出现新催化，盘前不可知：{headline[:80]}",
            ),
            evidence_news_published_utc=evidence["published_utc"],
            evidence_news_headline=headline,
        )
    if not news_complete:
        return _Attribution(
            MoverReason(
                "incomplete_evidence",
                "新闻证据源不可用，不能判断是催化剂漏抓还是纯因子缺口",
            )
        )
    if news_rows:
        return _Attribution(
            MoverReason(
                "factor_gap",
                "仅被涨幅榜、异动榜或多股汇总文章提及，未发现公司级原始催化；"
                "属于盘前价格动量/资金流覆盖缺口",
            )
        )
    return _Attribution(
        MoverReason(
            "factor_gap",
            "未发现新闻催化，偏技术、资金流或行业共振；当前催化优先模型不会选",
        )
    )


def classify_mover(
    symbol: str,
    *,
    gate_row: dict[str, Any] | None,
    news_rows: list[dict[str, Any]],
    selection_cutoff_utc: datetime,
    news_complete: bool = True,
) -> MoverReason:
    """Return an evidence-bounded root cause without using the symbol as a feature."""

    del symbol
    return _attribute_mover(
        gate_row=gate_row,
        news_rows=news_rows,
        selection_cutoff_utc=selection_cutoff_utc,
        news_complete=news_complete,
    ).reason


def _news_by_symbol(news: pl.DataFrame) -> dict[str, list[dict[str, Any]]]:
    if news.is_empty():
        return {}
    required = {"published_utc", "symbols", "headline"}
    missing = required.difference(news.columns)
    if missing:
        raise ValueError(f"news missing required columns: {sorted(missing)}")
    output: dict[str, list[dict[str, Any]]] = {}
    for row in news.iter_rows(named=True):
        published = row.get("published_utc")
        symbols = row.get("symbols")
        if not isinstance(published, datetime) or not isinstance(symbols, list):
            continue
        for symbol in symbols:
            output.setdefault(str(symbol).upper(), []).append(
                {
                    "published_utc": published,
                    "headline": str(row.get("headline") or "").strip(),
                    "symbol_count": len(symbols),
                }
            )
    return output


def _decision_outcome(category: str) -> str:
    return {
        "selected": "captured_opportunity",
        "intentional_gate": "intentional_rejection",
        "late_catalyst": "unpredictable_after_cutoff",
        "data_or_classifier_gap": "missed_detectable_opportunity",
        "factor_gap": "missed_detectable_opportunity",
        "incomplete_evidence": "incomplete_evidence",
    }[category]


def _pattern_key(category: str, gate_row: dict[str, Any] | None) -> str:
    if category == "intentional_gate":
        reject_reason = str((gate_row or {}).get("reject_reason") or "unknown_gate")
        return f"intentional_gate:{reject_reason}"
    return {
        "selected": "selected",
        "late_catalyst": "late_catalyst:after_cutoff",
        "data_or_classifier_gap": "data_or_classifier_gap:company_news_before_cutoff",
        "factor_gap": "factor_gap:price_order_flow_or_sector",
        "incomplete_evidence": "incomplete_evidence:news",
    }[category]


def _research_action(category: str) -> str:
    return {
        "selected": "none",
        "intentional_gate": "counterfactual_test_without_mutating_hard_guardrail",
        "late_catalyst": "none_unpredictable_at_cutoff",
        "data_or_classifier_gap": "audit_news_ingestion_entity_mapping_and_classifier",
        "factor_gap": "test_price_order_flow_or_sector_factor_in_sandbox",
        "incomplete_evidence": "restore_missing_evidence_before_attribution",
    }[category]


def build_intraday_selection_postmortem(
    *,
    trade_date: date,
    movers: pl.DataFrame,
    gates: pl.DataFrame,
    news: pl.DataFrame,
    news_complete: bool,
) -> pl.DataFrame:
    """Build immutable, ticker-anonymous-pattern labels for one completed session."""

    required_movers = {
        "symbol",
        "previous_close",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_return",
        "dollar_volume",
        "adv_usd",
        "atr_pct",
    }
    missing_movers = required_movers.difference(movers.columns)
    if missing_movers:
        raise ValueError(f"movers missing required columns: {sorted(missing_movers)}")
    if movers.is_empty():
        raise ValueError("movers must not be empty")
    symbols = [str(value).upper() for value in movers.get_column("symbol").to_list()]
    if len(symbols) != len(set(symbols)):
        raise ValueError("movers contain duplicate symbols")

    required_gates = {"symbol", "pass_gate", "selection_rank", "reject_reason", "gate_asof_utc"}
    missing_gates = required_gates.difference(gates.columns)
    if missing_gates:
        raise ValueError(f"gates missing required columns: {sorted(missing_gates)}")
    cutoff = gates.get_column("gate_asof_utc").max()
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None:
        raise ValueError("selection gate cutoff must be timezone-aware")
    gate_rows = {
        str(row["symbol"]).upper(): row for row in gates.iter_rows(named=True)
    }
    news_map = _news_by_symbol(news)

    ordered = movers.sort("close_return", descending=True)
    rows: list[dict[str, object]] = []
    for opportunity_rank, mover in enumerate(ordered.iter_rows(named=True), start=1):
        symbol = str(mover["symbol"]).upper()
        gate_row = gate_rows.get(symbol)
        attribution = _attribute_mover(
            gate_row=gate_row,
            news_rows=news_map.get(symbol, []),
            selection_cutoff_utc=cutoff,
            news_complete=news_complete,
        )
        category = attribution.reason.category
        previous_close = _number(mover.get("previous_close"))
        high = _number(mover.get("high"))
        low = _number(mover.get("low"))
        mfe = high / previous_close - 1 if high is not None and previous_close else None
        mae = low / previous_close - 1 if low is not None and previous_close else None
        selection_status = (
            "selected"
            if gate_row is not None and gate_row.get("pass_gate") is True
            else "rejected"
            if gate_row is not None
            else "not_seen"
        )
        rows.append(
            {
                "session_date": trade_date,
                "selection_cutoff_utc": cutoff,
                "opportunity_rank": opportunity_rank,
                "symbol": symbol,
                "previous_close": previous_close,
                "open": _number(mover.get("open")),
                "high": high,
                "low": low,
                "close": _number(mover.get("close")),
                "volume": _number(mover.get("volume")),
                "dollar_volume": _number(mover.get("dollar_volume")),
                "adv_usd": _number(mover.get("adv_usd")),
                "atr_pct": _number(mover.get("atr_pct")),
                "close_return": _number(mover.get("close_return")),
                "mfe_from_previous_close": mfe,
                "mae_from_previous_close": mae,
                "selection_status": selection_status,
                "pass_gate": gate_row.get("pass_gate") if gate_row is not None else None,
                "selection_rank": (
                    gate_row.get("selection_rank") if gate_row is not None else None
                ),
                "reject_reason": (
                    str(gate_row.get("reject_reason") or "") if gate_row is not None else None
                ),
                "decision_outcome": _decision_outcome(category),
                "root_cause": category,
                "root_cause_detail": attribution.reason.detail,
                "pattern_key": _pattern_key(category, gate_row),
                "research_action": _research_action(category),
                "research_eligible": category
                in {"intentional_gate", "data_or_classifier_gap", "factor_gap"},
                "production_change_allowed": False,
                "news_complete": news_complete,
                "evidence_news_published_utc": attribution.evidence_news_published_utc,
                "evidence_news_headline": attribution.evidence_news_headline,
                "schema_version": REVIEW_SCHEMA_VERSION,
                "provenance": (
                    "postclose_market_data|kernel.universe.selection_gates|"
                    "alpaca.news|point_in_time_attribution.v1"
                ),
            }
        )
    return pl.DataFrame(rows).sort("opportunity_rank", "symbol")
