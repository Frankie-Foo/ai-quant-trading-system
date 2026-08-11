"""Audited service implementation behind the MCP transport."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TypeVar

import polars as pl

from agent_gateway.contracts import (
    AgentRole,
    AuditReport,
    Availability,
    EvolutionProposal,
    Fact,
    Lesson,
    QueryEntity,
    StoreQuery,
    Thesis,
    ToolEnvelope,
    now_utc,
)
from agent_gateway.policy import authorize
from agent_gateway.snapshots import LoadedSnapshot, SnapshotRepository
from agent_gateway.store import AgentFactStore, build_agent_fact_store
from data_plane.calendar import build_xnys_schedule
from kernel.config import load_config
from kernel.exits import make_exits
from kernel.sizing import size_position
from kernel.tradeplan import TradePlan

T = TypeVar("T")
TECHNICAL_TOKEN_ALLOWLIST = frozenset(
    {"ATR", "RVOL", "VWAP", "FINRA", "SIP", "LULD", "PDCA", "GO", "NO"}
)
STORE_ENTITIES = frozenset(
    {
        QueryEntity.THESES,
        QueryEntity.AUDIT_REPORTS,
        QueryEntity.LESSONS,
        QueryEntity.PROPOSALS,
        QueryEntity.TRADEPLAN_DRAFTS,
        QueryEntity.TOOL_AUDIT,
    }
)


class AgentGatewayService:
    """Fail-closed tools: no method can access a broker or executable OMS table."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        data_root: str | Path | None = None,
        store: AgentFactStore | None = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.snapshots = SnapshotRepository(data_root or self.project_root / "data")
        self.store = store or build_agent_fact_store(self.project_root)
        self.store.initialize()
        self.config = load_config(self.project_root / "config.yaml")

    def _execute(
        self,
        *,
        tool: str,
        agent_name: str,
        request: object,
        handler: Callable[[AgentRole], T],
    ) -> T:
        actor: AgentRole | None = None
        audit_request = {"claimed_agent": agent_name, "arguments": request}
        try:
            actor = authorize(agent_name, tool)
            response = handler(actor)
        except Exception as exc:
            self.store.record_audit(
                actor=actor,
                tool=tool,
                request=audit_request,
                response=None,
                success=False,
                error_code=type(exc).__name__,
            )
            raise
        self.store.record_audit(
            actor=actor,
            tool=tool,
            request=audit_request,
            response=response,
            success=True,
            error_code=None,
        )
        return response

    @staticmethod
    def _scalar(value: object) -> bool | int | float | str | None:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        raise ValueError(f"unsupported fact value type: {type(value).__name__}")

    @staticmethod
    def _required_float(row: Mapping[str, object], name: str) -> float:
        value = row.get(name)
        if not isinstance(value, (int, float)):
            raise ValueError(f"required numeric feature is unavailable: {name}")
        return float(value)

    @staticmethod
    def _fact(
        row: Mapping[str, object],
        *,
        name: str,
        provenance_field: str | None,
        fallback_provenance: str,
        asof_utc: datetime,
    ) -> Fact:
        value = row.get(name)
        provenance = row.get(provenance_field) if provenance_field else None
        return Fact(
            name=name,
            value=AgentGatewayService._scalar(value),
            availability=(
                Availability.AVAILABLE if value is not None else Availability.UNAVAILABLE
            ),
            provenance=str(provenance or fallback_provenance),
            asof_utc=asof_utc,
        )

    @staticmethod
    def _envelope(
        *,
        tool: str,
        loaded: LoadedSnapshot | None,
        data: dict[str, object] | list[dict[str, object]],
        availability: Availability = Availability.AVAILABLE,
        provenance: str | None = None,
        snapshot_ids: tuple[str, ...] | None = None,
    ) -> dict[str, object]:
        envelope = ToolEnvelope(
            tool=tool,
            asof_utc=now_utc(),
            availability=availability,
            provenance=provenance
            or (loaded.manifest.source if loaded else f"agent_gateway.{tool}.v1"),
            snapshot_ids=(
                snapshot_ids
                if snapshot_ids is not None
                else (loaded.manifest.dataset_id,)
                if loaded
                else ()
            ),
            data=data,
        )
        return envelope.model_dump(mode="json")

    def _anonymous_case(self, symbol: str) -> str:
        salt = os.getenv("QUANT_AGENT_ANONYMIZATION_SALT", "").strip() or "agent-gateway-v1"
        digest = hashlib.sha256(f"{salt}|{symbol.upper()}".encode()).hexdigest()
        return f"case-{digest[:16]}"

    def _redact_for_pdca(self, value: object) -> object:
        # Query rows already carry the symbols that need anonymizing. Building a
        # repository-wide symbol index here makes every PDCA query rescan hundreds
        # of parquet snapshots and can stall the postmarket scheduler.
        known: set[str] = set()

        def collect_symbols(item: object) -> None:
            if isinstance(item, dict):
                symbol = item.get("symbol")
                if isinstance(symbol, str):
                    known.add(symbol.strip().upper())
                for nested in item.values():
                    collect_symbols(nested)
            elif isinstance(item, list | tuple):
                for nested in item:
                    collect_symbols(nested)

        collect_symbols(value)
        known -= TECHNICAL_TOKEN_ALLOWLIST
        mapping = {symbol: self._anonymous_case(symbol) for symbol in known if len(symbol) >= 2}

        def redact(item: object) -> object:
            if isinstance(item, dict):
                output: dict[str, object] = {}
                for key, nested in item.items():
                    if key == "symbol" and isinstance(nested, str):
                        output["case_id"] = mapping.get(
                            nested.upper(), self._anonymous_case(nested)
                        )
                    else:
                        output[str(key)] = redact(nested)
                return output
            if isinstance(item, list):
                return [redact(nested) for nested in item]
            if isinstance(item, tuple):
                return [redact(nested) for nested in item]
            if isinstance(item, str):
                text = item
                for symbol, case_id in mapping.items():
                    text = re.sub(
                        rf"(?<![A-Z0-9]){re.escape(symbol)}(?![A-Z0-9])",
                        case_id,
                        text,
                        flags=re.IGNORECASE,
                    )
                return text
            return item

        return redact(value)

    def _snapshot_rows(self, loaded: LoadedSnapshot, *, limit: int) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for row in loaded.frame.head(limit).to_dicts():
            facts: list[dict[str, object]] = []
            attributes: dict[str, object] = {}
            for name, value in row.items():
                if isinstance(value, bool | int | float) or value is None:
                    provenance_name = f"{name}_provenance"
                    provenance = row.get(provenance_name)
                    facts.append(
                        Fact(
                            name=name,
                            value=self._scalar(value),
                            availability=(
                                Availability.AVAILABLE
                                if value is not None
                                else Availability.UNAVAILABLE
                            ),
                            provenance=str(
                                provenance or f"{loaded.manifest.dataset_id}|field:{name}"
                            ),
                            asof_utc=loaded.manifest.asof_utc,
                        ).model_dump(mode="json")
                    )
                elif isinstance(value, datetime | date):
                    attributes[name] = value.isoformat()
                else:
                    attributes[name] = value
            attributes["facts"] = facts
            rows.append(attributes)
        return rows

    def _snapshot_query(
        self, *, source: str, query: StoreQuery
    ) -> tuple[LoadedSnapshot | None, list[dict[str, object]], Availability]:
        try:
            loaded = self.snapshots.latest_for_source(source, trade_date=query.trade_date)
        except LookupError:
            return None, [], Availability.UNAVAILABLE
        return loaded, self._snapshot_rows(loaded, limit=query.limit), Availability.AVAILABLE

    def _snapshot_history_query(
        self,
        *,
        source: str,
        limit: int,
    ) -> tuple[tuple[LoadedSnapshot, ...], list[dict[str, object]], Availability]:
        loaded = self.snapshots.history_for_source(source, row_limit=limit)
        if not loaded:
            return (), [], Availability.UNAVAILABLE
        rows = [
            row
            for snapshot in loaded
            for row in self._snapshot_rows(snapshot, limit=limit)
        ][:limit]
        return loaded, rows, Availability.AVAILABLE

    def _ledger_query(
        self, *, entity: QueryEntity, trade_date: date | None, limit: int
    ) -> tuple[list[dict[str, object]], Availability]:
        configured = os.getenv("QUANT_AGENT_ORDER_LEDGER", "").strip()
        path = Path(configured) if configured else self.project_root / "runs" / "orders.sqlite3"
        if not path.exists():
            return [], Availability.UNAVAILABLE
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        table = "trade_plans" if entity is QueryEntity.TRADE_PLANS else "orders"
        column = "plan_json" if entity is QueryEntity.TRADE_PLANS else "lifecycle_json"
        clauses = ""
        params: list[object] = []
        if trade_date is not None:
            if entity is QueryEntity.TRADE_PLANS:
                clauses = " WHERE trade_date = ?"
            else:
                clauses = " WHERE plan_id IN (SELECT plan_id FROM trade_plans WHERE trade_date = ?)"
            params.append(trade_date.isoformat())
        params.append(limit)
        try:
            with sqlite3.connect(uri, uri=True) as connection:
                rows = connection.execute(
                    f"SELECT {column} FROM {table}{clauses} ORDER BY created_at_utc DESC LIMIT ?",
                    params,
                ).fetchall()
        except sqlite3.Error:
            return [], Availability.UNAVAILABLE
        return [json.loads(str(row[0])) for row in rows], Availability.AVAILABLE

    def universe_query(
        self,
        *,
        agent_name: str,
        trade_date: date,
        pool: str = "passing",
        limit: int = 100,
    ) -> dict[str, object]:
        request = {"trade_date": trade_date.isoformat(), "pool": pool, "limit": limit}

        def handler(_: AgentRole) -> dict[str, object]:
            if pool not in {"passing", "ranked", "all"}:
                raise ValueError("pool must be passing, ranked, or all")
            if not 1 <= limit <= 200:
                raise ValueError("limit must be in [1, 200]")
            loaded = self.snapshots.selection_for_date(trade_date)
            frame = loaded.frame
            if pool == "passing":
                frame = frame.filter(frame["pass_gate"])
            elif pool == "ranked":
                frame = frame.filter(frame["selection_rank"].is_not_null())
            frame = frame.sort("selection_rank", nulls_last=True).head(limit)
            rows: list[dict[str, object]] = []
            for row in frame.to_dicts():
                facts = [
                    self._fact(
                        row,
                        name=name,
                        provenance_field=provenance_field,
                        fallback_provenance=f"{loaded.manifest.dataset_id}|{name}",
                        asof_utc=loaded.manifest.asof_utc,
                    ).model_dump(mode="json")
                    for name, provenance_field in (
                        ("selection_rank", None),
                        ("rvol", "rvol_provenance"),
                        ("price", "price_provenance"),
                        ("adv_usd", "adv_usd_provenance"),
                        ("beta", "beta_provenance"),
                        ("atr_pct", "atr_pct_provenance"),
                        ("market_cap", "market_cap_provenance"),
                        ("free_float", "free_float_provenance"),
                    )
                ]
                rows.append(
                    {
                        "symbol": row["symbol"],
                        "catalyst_categories": row.get("catalyst_categories", []),
                        "pass_gate": row.get("pass_gate"),
                        "reject_reason": row.get("reject_reason"),
                        "facts": facts,
                    }
                )
            return self._envelope(tool="universe_query", loaded=loaded, data=rows)

        return self._execute(
            tool="universe_query", agent_name=agent_name, request=request, handler=handler
        )

    def features_momentum(
        self, *, agent_name: str, trade_date: date, symbol: str
    ) -> dict[str, object]:
        request = {"trade_date": trade_date.isoformat(), "symbol": symbol}

        def handler(_: AgentRole) -> dict[str, object]:
            loaded, row = self.snapshots.selection_row(trade_date, symbol)
            facts = [
                self._fact(
                    row,
                    name=name,
                    provenance_field=provenance,
                    fallback_provenance=f"{loaded.manifest.dataset_id}|{name}",
                    asof_utc=loaded.manifest.asof_utc,
                )
                for name, provenance in (
                    ("rvol", "rvol_provenance"),
                    ("beta", "beta_provenance"),
                    ("atr_pct", "atr_pct_provenance"),
                    ("price", "price_provenance"),
                )
            ]
            price, atr_pct = row.get("price"), row.get("atr_pct")
            atr_value = (
                float(price) * float(atr_pct)
                if isinstance(price, (int, float)) and isinstance(atr_pct, (int, float))
                else None
            )
            facts.append(
                Fact(
                    name="atr14",
                    value=atr_value,
                    availability=(
                        Availability.AVAILABLE
                        if atr_value is not None
                        else Availability.UNAVAILABLE
                    ),
                    provenance=(
                        f"{row.get('price_provenance')}|{row.get('atr_pct_provenance')}|derived:price*atr_pct"
                    ),
                    asof_utc=loaded.manifest.asof_utc,
                )
            )
            facts.append(
                Fact(
                    name="days_in_play",
                    value=None,
                    availability=Availability.UNAVAILABLE,
                    provenance="agent_gateway.features_momentum|history_feature_not_materialized",
                    asof_utc=loaded.manifest.asof_utc,
                )
            )
            data: dict[str, object] = {
                "symbol": symbol.strip().upper(),
                "facts": [fact.model_dump(mode="json") for fact in facts],
            }
            return self._envelope(tool="features_momentum", loaded=loaded, data=data)

        return self._execute(
            tool="features_momentum", agent_name=agent_name, request=request, handler=handler
        )

    def features_liquidity(
        self, *, agent_name: str, trade_date: date, symbol: str
    ) -> dict[str, object]:
        request = {"trade_date": trade_date.isoformat(), "symbol": symbol}

        def handler(_: AgentRole) -> dict[str, object]:
            loaded, row = self.snapshots.selection_row(trade_date, symbol)
            facts = [
                self._fact(
                    row,
                    name=name,
                    provenance_field=provenance,
                    fallback_provenance=f"{loaded.manifest.dataset_id}|{name}",
                    asof_utc=loaded.manifest.asof_utc,
                )
                for name, provenance in (
                    ("adv_usd", "adv_usd_provenance"),
                    ("market_cap", "market_cap_provenance"),
                    ("free_float", "free_float_provenance"),
                    ("tier", "market_cap_provenance"),
                )
            ]
            adv, market_cap = row.get("adv_usd"), row.get("market_cap")
            turnover = (
                float(adv) / float(market_cap)
                if isinstance(adv, (int, float))
                and isinstance(market_cap, (int, float))
                and market_cap > 0
                else None
            )
            facts.append(
                Fact(
                    name="turnover",
                    value=turnover,
                    availability=(
                        Availability.AVAILABLE if turnover is not None else Availability.UNAVAILABLE
                    ),
                    provenance=(
                        f"{row.get('adv_usd_provenance')}|{row.get('market_cap_provenance')}|derived:adv_usd/market_cap"
                    ),
                    asof_utc=loaded.manifest.asof_utc,
                )
            )
            for name in ("zero_trade_fraction", "spread_estimate"):
                facts.append(
                    Fact(
                        name=name,
                        value=None,
                        availability=Availability.UNAVAILABLE,
                        provenance=f"agent_gateway.features_liquidity|{name}_not_materialized",
                        asof_utc=loaded.manifest.asof_utc,
                    )
                )
            data: dict[str, object] = {
                "symbol": symbol.strip().upper(),
                "facts": [fact.model_dump(mode="json") for fact in facts],
            }
            return self._envelope(tool="features_liquidity", loaded=loaded, data=data)

        return self._execute(
            tool="features_liquidity", agent_name=agent_name, request=request, handler=handler
        )

    def _unavailable_feature(
        self, *, tool: str, agent_name: str, trade_date: date, symbol: str, names: tuple[str, ...]
    ) -> dict[str, object]:
        request = {"trade_date": trade_date.isoformat(), "symbol": symbol}

        def handler(_: AgentRole) -> dict[str, object]:
            loaded, _row = self.snapshots.selection_row(trade_date, symbol)
            facts = [
                Fact(
                    name=name,
                    value=None,
                    availability=Availability.UNAVAILABLE,
                    provenance=f"agent_gateway.{tool}|deterministic_source_not_configured",
                    asof_utc=loaded.manifest.asof_utc,
                ).model_dump(mode="json")
                for name in names
            ]
            return self._envelope(
                tool=tool,
                loaded=loaded,
                data={"symbol": symbol.strip().upper(), "facts": facts},
                availability=Availability.UNAVAILABLE,
                provenance=f"agent_gateway.{tool}|explicit_degradation",
            )

        return self._execute(tool=tool, agent_name=agent_name, request=request, handler=handler)

    def features_order_flow(
        self, *, agent_name: str, trade_date: date, symbol: str
    ) -> dict[str, object]:
        request = {"trade_date": trade_date.isoformat(), "symbol": symbol}

        def handler(_: AgentRole) -> dict[str, object]:
            normalized = symbol.strip().upper()
            selection_loaded, _selection_row = self.snapshots.selection_row(
                trade_date, normalized
            )
            try:
                loaded = self.snapshots.latest_for_source(
                    "kernel.features.order_flow_shadow",
                    trade_date=trade_date,
                )
            except LookupError:
                loaded = None
            rows = (
                []
                if loaded is None
                else loaded.frame.filter(pl.col("symbol") == normalized).to_dicts()
            )
            if len(rows) != 1:
                names = (
                    "order_imbalance",
                    "vpoc",
                    "buy_sell_pressure_ratio",
                    "quote_size_imbalance",
                    "microprice",
                    "spread_bps",
                    "order_flow_confirmation_score",
                )
                unavailable_facts = [
                    Fact(
                        name=name,
                        value=None,
                        availability=Availability.UNAVAILABLE,
                        provenance=(
                            "agent_gateway.features_order_flow|"
                            "shadow_snapshot_not_materialized"
                        ),
                        asof_utc=selection_loaded.manifest.asof_utc,
                    ).model_dump(mode="json")
                    for name in names
                ]
                return self._envelope(
                    tool="features_order_flow",
                    loaded=selection_loaded,
                    data={"symbol": normalized, "facts": unavailable_facts},
                    availability=Availability.UNAVAILABLE,
                    provenance="agent_gateway.features_order_flow|explicit_degradation",
                )

            row = rows[0]
            if loaded is None:
                raise RuntimeError("order-flow snapshot resolution is inconsistent")
            cutoff = row.get("data_cutoff_utc")
            fact_asof = (
                cutoff
                if isinstance(cutoff, datetime)
                else loaded.manifest.asof_utc
            )
            names = (
                "order_imbalance",
                "vpoc",
                "buy_sell_pressure_ratio",
                "quote_size_imbalance",
                "microprice",
                "spread_bps",
                "order_flow_confirmation_score",
            )
            available_facts = [
                self._fact(
                    row,
                    name=name,
                    provenance_field="order_flow_provenance",
                    fallback_provenance=(
                        "agent_gateway.features_order_flow|metric_unavailable"
                    ),
                    asof_utc=fact_asof,
                )
                for name in names
            ]
            availability = (
                Availability.AVAILABLE
                if any(
                    fact.availability is Availability.AVAILABLE
                    for fact in available_facts
                )
                else Availability.UNAVAILABLE
            )
            return self._envelope(
                tool="features_order_flow",
                loaded=loaded,
                data={
                    "symbol": normalized,
                    "source_availability": row.get("availability"),
                    "facts": [
                        fact.model_dump(mode="json") for fact in available_facts
                    ],
                },
                availability=availability,
                provenance=str(
                    row.get("order_flow_provenance")
                    or "agent_gateway.features_order_flow|unknown_provenance"
                ),
            )

        return self._execute(
            tool="features_order_flow",
            agent_name=agent_name,
            request=request,
            handler=handler,
        )

    def features_short_flow(
        self, *, agent_name: str, trade_date: date, symbol: str
    ) -> dict[str, object]:
        return self._unavailable_feature(
            tool="features_short_flow",
            agent_name=agent_name,
            trade_date=trade_date,
            symbol=symbol,
            names=("finra_short_volume_ratio",),
        )

    def features_sentiment(
        self, *, agent_name: str, trade_date: date, symbol: str
    ) -> dict[str, object]:
        request = {"trade_date": trade_date.isoformat(), "symbol": symbol}

        def handler(_: AgentRole) -> dict[str, object]:
            loaded, row = self.snapshots.selection_row(trade_date, symbol)
            fact = self._fact(
                row,
                name="model_score",
                provenance_field="model_provenance",
                fallback_provenance="agent_gateway.features_sentiment|frozen_score_unavailable",
                asof_utc=loaded.manifest.asof_utc,
            )
            availability = fact.availability
            return self._envelope(
                tool="features_sentiment",
                loaded=loaded,
                data={"symbol": symbol.strip().upper(), "facts": [fact.model_dump(mode="json")]},
                availability=availability,
            )

        return self._execute(
            tool="features_sentiment", agent_name=agent_name, request=request, handler=handler
        )

    def sizing_preview(
        self,
        *,
        agent_name: str,
        trade_date: date,
        symbol: str,
        confidence: float,
        confidence_provenance: str,
    ) -> dict[str, object]:
        request = {
            "trade_date": trade_date.isoformat(),
            "symbol": symbol,
            "confidence": Fact(
                name="confidence",
                value=confidence,
                availability=Availability.AVAILABLE,
                provenance=confidence_provenance,
            ).model_dump(mode="json"),
        }

        def handler(_: AgentRole) -> dict[str, object]:
            loaded, row = self.snapshots.selection_row(trade_date, symbol)
            price = self._required_float(row, "price")
            atr14 = price * self._required_float(row, "atr_pct")
            result = size_position(
                symbol=symbol.strip().upper(),
                price=price,
                atr14=atr14,
                adv_usd=self._required_float(row, "adv_usd"),
                tier=str(row["tier"]),
                confidence=confidence,
                cfg=self.config,
            )
            facts = [
                Fact(
                    name=name,
                    value=self._scalar(value),
                    availability=Availability.AVAILABLE,
                    provenance=result.provenance,
                    asof_utc=loaded.manifest.asof_utc,
                ).model_dump(mode="json")
                for name, value in asdict(result).items()
                if name not in {"symbol", "provenance", "binding_cap"}
            ]
            data: dict[str, object] = {
                "symbol": result.symbol,
                "binding_cap": result.binding_cap,
                "facts": facts,
            }
            return self._envelope(
                tool="sizing_preview",
                loaded=loaded,
                data=data,
                provenance=result.provenance,
            )

        return self._execute(
            tool="sizing_preview", agent_name=agent_name, request=request, handler=handler
        )

    def exits_preview(self, *, agent_name: str, trade_date: date, symbol: str) -> dict[str, object]:
        request = {"trade_date": trade_date.isoformat(), "symbol": symbol}

        def handler(_: AgentRole) -> dict[str, object]:
            loaded, row = self.snapshots.selection_row(trade_date, symbol)
            schedule = build_xnys_schedule(trade_date, trade_date)
            if schedule.height != 1:
                raise ValueError("trade date is not an XNYS session")
            price = self._required_float(row, "price")
            plan = make_exits(
                price,
                price * self._required_float(row, "atr_pct"),
                trade_date=trade_date,
                is_half_day=bool(schedule.row(0, named=True)["is_half_day"]),
                cfg=self.config,
            )
            facts = [
                Fact(
                    name=name,
                    value=value.isoformat() if isinstance(value, datetime) else value,
                    availability=Availability.AVAILABLE,
                    provenance=plan.provenance,
                    asof_utc=loaded.manifest.asof_utc,
                ).model_dump(mode="json")
                for name, value in asdict(plan).items()
                if name != "provenance"
            ]
            return self._envelope(
                tool="exits_preview",
                loaded=loaded,
                data={"symbol": symbol.strip().upper(), "facts": facts},
                provenance=plan.provenance,
            )

        return self._execute(
            tool="exits_preview", agent_name=agent_name, request=request, handler=handler
        )

    def tradeplan_submit(
        self,
        *,
        agent_name: str,
        trade_date: date,
        symbol: str,
        confidence: float,
        confidence_provenance: str,
    ) -> dict[str, object]:
        request = {
            "trade_date": trade_date.isoformat(),
            "symbol": symbol,
            "confidence": Fact(
                name="confidence",
                value=confidence,
                availability=Availability.AVAILABLE,
                provenance=confidence_provenance,
            ).model_dump(mode="json"),
        }

        def handler(actor: AgentRole) -> dict[str, object]:
            loaded, row = self.snapshots.selection_row(trade_date, symbol)
            if not bool(row.get("pass_gate")) or row.get("selection_rank") is None:
                raise ValueError("symbol is not in the ranked, hard-gate-passing pool")
            price = self._required_float(row, "price")
            atr14 = price * self._required_float(row, "atr_pct")
            sizing = size_position(
                symbol=symbol.strip().upper(),
                price=price,
                atr14=atr14,
                adv_usd=self._required_float(row, "adv_usd"),
                tier=str(row["tier"]),
                confidence=confidence,
                cfg=self.config,
            )
            if sizing.shares <= 0:
                raise ValueError("deterministic sizing produced zero shares")
            schedule = build_xnys_schedule(trade_date, trade_date)
            if schedule.height != 1:
                raise ValueError("trade date is not an XNYS session")
            exits = make_exits(
                price,
                atr14,
                trade_date=trade_date,
                is_half_day=bool(schedule.row(0, named=True)["is_half_day"]),
                cfg=self.config,
            )
            created = now_utc()
            if exits.time_stop_utc <= created:
                raise ValueError("time stop has passed; fail-closed draft rejection")
            seed = (
                f"{loaded.manifest.dataset_id}|{symbol.upper()}|"
                f"{confidence}|{confidence_provenance}"
            )
            plan_id = f"agent-shadow-{hashlib.sha256(seed.encode()).hexdigest()[:20]}"
            plan = TradePlan(
                plan_id=plan_id,
                trace_id=plan_id,
                strategy_version="agent-shadow.v1",
                symbol=symbol,
                trade_date=trade_date,
                decision_asof_utc=min(created, loaded.manifest.asof_utc),
                created_at_utc=created,
                quantity=sizing.shares,
                reference_price=Decimal(str(price)),
                take_profit_price=Decimal(str(exits.tp_px)),
                stop_loss_price=Decimal(str(exits.sl_px)),
                time_stop_utc=exits.time_stop_utc,
                source_snapshot_ids=(loaded.manifest.dataset_id,),
                provenance=(
                    f"agent_gateway.tradeplan_submit.shadow_only|{sizing.provenance}|{exits.provenance}|"
                    f"confidence:{confidence_provenance}"
                ),
            )
            document: dict[str, object] = {
                **plan.model_dump(mode="json"),
                "status": "shadow_draft",
                "execution_eligible": False,
                "guardrail_state": "deferred_and_fail_closed",
                "broker_submission_count": 0,
            }
            record_id = self.store.put_tradeplan_draft(actor=actor, document=document)
            return self._envelope(
                tool="tradeplan_submit",
                loaded=loaded,
                data={
                    "record_id": record_id,
                    "status": "shadow_draft",
                    "execution_eligible": False,
                    "guardrail_state": "deferred_and_fail_closed",
                    "broker_submission_count": 0,
                    "plan": document,
                },
                provenance=str(document["provenance"]),
            )

        return self._execute(
            tool="tradeplan_submit", agent_name=agent_name, request=request, handler=handler
        )

    def theses_write(self, *, agent_name: str, thesis: Thesis) -> dict[str, object]:
        def handler(actor: AgentRole) -> dict[str, object]:
            if thesis.agent is not actor:
                raise PermissionError("agents may only write theses under their own identity")
            record_id = self.store.put_thesis(thesis)
            return self._envelope(
                tool="theses_write",
                loaded=None,
                data={"record_id": record_id, "status": "shadow"},
                provenance="agent_gateway.store.agent_theses.v1",
            )

        return self._execute(
            tool="theses_write", agent_name=agent_name, request=thesis, handler=handler
        )

    def lessons_write(self, *, agent_name: str, lesson: Lesson) -> dict[str, object]:
        def handler(actor: AgentRole) -> dict[str, object]:
            if lesson.agent is not actor or actor is not AgentRole.PDCA:
                raise PermissionError("only pdca may write lessons under its own identity")
            narrative = " ".join(
                (
                    lesson.hypothesis,
                    lesson.observation,
                    lesson.conclusion,
                    *lesson.factor_profile,
                )
            )
            try:
                known = {
                    str(value).upper()
                    for value in self.snapshots.selection_for_date(lesson.trade_date)
                    .frame.get_column("symbol")
                    .to_list()
                }
            except LookupError:
                known = set()
            known -= TECHNICAL_TOKEN_ALLOWLIST
            hits = sorted(
                symbol
                for symbol in known
                if len(symbol) >= 2
                and re.search(
                    rf"(?<![A-Z0-9]){re.escape(symbol)}(?![A-Z0-9])",
                    narrative.upper(),
                )
            )
            if hits:
                raise ValueError("lesson narrative must be ticker-anonymous")
            record_id = self.store.put_lesson(lesson)
            return self._envelope(
                tool="lessons_write",
                loaded=None,
                data={"record_id": record_id, "status": "accepted_fact"},
                provenance="agent_gateway.store.lessons.v1",
            )

        return self._execute(
            tool="lessons_write", agent_name=agent_name, request=lesson, handler=handler
        )

    def audit_reports_write(self, *, agent_name: str, report: AuditReport) -> dict[str, object]:
        def handler(actor: AgentRole) -> dict[str, object]:
            if report.agent is not actor or actor is not AgentRole.DISCIPLINE:
                raise PermissionError(
                    "only discipline may write audit reports under its own identity"
                )
            record_id = self.store.put_audit_report(report)
            return self._envelope(
                tool="audit_reports_write",
                loaded=None,
                data={"record_id": record_id, "status": report.status},
                provenance="agent_gateway.store.audit_reports.v1",
            )

        return self._execute(
            tool="audit_reports_write",
            agent_name=agent_name,
            request=report,
            handler=handler,
        )

    def proposal_write(self, *, agent_name: str, proposal: EvolutionProposal) -> dict[str, object]:
        def handler(actor: AgentRole) -> dict[str, object]:
            if proposal.agent is not actor:
                raise PermissionError("agents may only write proposals under their own identity")
            record_id = self.store.put_proposal(proposal)
            return self._envelope(
                tool="proposal_write",
                loaded=None,
                data={
                    "record_id": record_id,
                    "status": "draft",
                    "production_eligible": False,
                },
                provenance="agent_gateway.store.evolution_proposals.v1",
            )

        return self._execute(
            tool="proposal_write", agent_name=agent_name, request=proposal, handler=handler
        )

    def postgres_query(self, *, agent_name: str, query: StoreQuery) -> dict[str, object]:
        def handler(actor: AgentRole) -> dict[str, object]:
            loaded: LoadedSnapshot | None = None
            history_snapshots: tuple[LoadedSnapshot, ...] = ()
            availability = Availability.AVAILABLE
            if query.entity in STORE_ENTITIES:
                rows = self.store.query(query)
            elif query.entity is QueryEntity.TRADING_EPISODES:
                loaded, rows, availability = self._snapshot_query(
                    source="research.trading_episodes", query=query
                )
            elif query.entity is QueryEntity.INTRADAY_SELECTION_POSTMORTEMS:
                if query.trade_date is None:
                    history_snapshots, rows, availability = (
                        self._snapshot_history_query(
                            source="research.intraday_selection_postmortem",
                            limit=query.limit,
                        )
                    )
                else:
                    loaded, rows, availability = self._snapshot_query(
                        source="research.intraday_selection_postmortem",
                        query=query,
                    )
            elif query.entity is QueryEntity.UNIVERSE_SNAPSHOTS:
                loaded, rows, availability = self._snapshot_query(
                    source="kernel.universe.selection_gates", query=query
                )
            elif query.entity is QueryEntity.FACTOR_SNAPSHOTS:
                loaded, rows, availability = self._snapshot_query(
                    source="research.factor_snapshots", query=query
                )
            elif query.entity in {QueryEntity.TRADE_PLANS, QueryEntity.EXECUTIONS}:
                rows, availability = self._ledger_query(
                    entity=query.entity, trade_date=query.trade_date, limit=query.limit
                )
            else:
                rows, availability = [], Availability.UNAVAILABLE
            if actor is AgentRole.PDCA:
                redacted = self._redact_for_pdca(rows)
                if not isinstance(redacted, list):
                    raise TypeError("PDCA query redaction produced an invalid result")
                rows = [dict(item) for item in redacted if isinstance(item, dict)]
            return self._envelope(
                tool="postgres_query",
                loaded=loaded,
                data=rows,
                availability=availability,
                provenance=(
                    "agent_gateway.store.parameterized_allowlist.v1"
                    if loaded is None and not history_snapshots
                    else f"{loaded.manifest.dataset_id}|parameterized_snapshot_query"
                    if loaded is not None
                    else "research.intraday_selection_postmortem|history_snapshot_query.v1"
                ),
                snapshot_ids=(
                    tuple(
                        snapshot.manifest.dataset_id
                        for snapshot in history_snapshots
                    )
                    if history_snapshots
                    else None
                ),
            )

        return self._execute(
            tool="postgres_query", agent_name=agent_name, request=query, handler=handler
        )
