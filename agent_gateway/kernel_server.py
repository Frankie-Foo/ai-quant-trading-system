"""Official MCP stdio transport for the audited quant-agent gateway."""

from __future__ import annotations

import os
from datetime import date
from functools import lru_cache
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from agent_gateway.contracts import AuditReport, EvolutionProposal, Lesson, StoreQuery, Thesis
from agent_gateway.service import AgentGatewayService

mcp = FastMCP(
    "Quant Agent Kernel",
    instructions=(
        "Slow-loop research tools only. Every numeric fact carries provenance. "
        "Unavailable data is N/A. Trade-plan submissions are non-executable shadow drafts."
    ),
    json_response=True,
)


@lru_cache(maxsize=1)
def get_service() -> AgentGatewayService:
    default_root = Path(__file__).resolve().parents[1]
    project_root = Path(os.getenv("TRADING_SYSTEM_ROOT", str(default_root)))
    data_root_text = os.getenv("QUANT_AGENT_DATA_ROOT", "").strip()
    state_db_text = os.getenv("QUANT_AGENT_STATE_DB", "").strip()
    if state_db_text:
        from agent_gateway.store import SQLiteAgentFactStore

        return AgentGatewayService(
            project_root=project_root,
            data_root=Path(data_root_text) if data_root_text else None,
            store=SQLiteAgentFactStore(state_db_text),
        )
    return AgentGatewayService(
        project_root=project_root,
        data_root=Path(data_root_text) if data_root_text else None,
    )


def _date(value: str) -> date:
    return date.fromisoformat(value)


@mcp.tool()
def universe_query(
    agent_name: str, trade_date: str, pool: str = "passing", limit: int = 100
) -> dict[str, object]:
    """Read a bounded accepted selection pool for one XNYS trade date."""
    return get_service().universe_query(
        agent_name=agent_name,
        trade_date=_date(trade_date),
        pool=pool,
        limit=limit,
    )


@mcp.tool()
def features_momentum(agent_name: str, trade_date: str, symbol: str) -> dict[str, object]:
    """Read frozen price, RVOL, beta and ATR facts; missing history is explicit N/A."""
    return get_service().features_momentum(
        agent_name=agent_name, trade_date=_date(trade_date), symbol=symbol
    )


@mcp.tool()
def features_liquidity(agent_name: str, trade_date: str, symbol: str) -> dict[str, object]:
    """Read frozen ADV, market-cap, float and turnover facts."""
    return get_service().features_liquidity(
        agent_name=agent_name, trade_date=_date(trade_date), symbol=symbol
    )


@mcp.tool()
def features_order_flow(agent_name: str, trade_date: str, symbol: str) -> dict[str, object]:
    """Read order-flow facts or explicit N/A when no deterministic feed is configured."""
    return get_service().features_order_flow(
        agent_name=agent_name, trade_date=_date(trade_date), symbol=symbol
    )


@mcp.tool()
def features_short_flow(agent_name: str, trade_date: str, symbol: str) -> dict[str, object]:
    """Read long-risk short-flow facts; this tool can never create a short trade."""
    return get_service().features_short_flow(
        agent_name=agent_name, trade_date=_date(trade_date), symbol=symbol
    )


@mcp.tool()
def features_sentiment(agent_name: str, trade_date: str, symbol: str) -> dict[str, object]:
    """Read only a previously frozen sentiment score; never call a model in the kernel."""
    return get_service().features_sentiment(
        agent_name=agent_name, trade_date=_date(trade_date), symbol=symbol
    )


@mcp.tool()
def sizing_preview(
    agent_name: str,
    trade_date: str,
    symbol: str,
    confidence: float,
    confidence_provenance: str,
) -> dict[str, object]:
    """Run deterministic three-cap sizing without creating an order instruction."""
    return get_service().sizing_preview(
        agent_name=agent_name,
        trade_date=_date(trade_date),
        symbol=symbol,
        confidence=confidence,
        confidence_provenance=confidence_provenance,
    )


@mcp.tool()
def exits_preview(agent_name: str, trade_date: str, symbol: str) -> dict[str, object]:
    """Run deterministic ATR and exchange-calendar exit calculations."""
    return get_service().exits_preview(
        agent_name=agent_name, trade_date=_date(trade_date), symbol=symbol
    )


@mcp.tool()
def tradeplan_submit(
    agent_name: str,
    trade_date: str,
    symbol: str,
    confidence: float,
    confidence_provenance: str,
) -> dict[str, object]:
    """Store a schema-checked shadow draft; never write to OMS or call a broker."""
    return get_service().tradeplan_submit(
        agent_name=agent_name,
        trade_date=_date(trade_date),
        symbol=symbol,
        confidence=confidence,
        confidence_provenance=confidence_provenance,
    )


@mcp.tool()
def postgres_query(agent_name: str, query: StoreQuery) -> dict[str, object]:
    """Run a parameterized allowlisted fact-store query; arbitrary SQL is impossible."""
    return get_service().postgres_query(agent_name=agent_name, query=query)


@mcp.tool()
def theses_write(agent_name: str, thesis: Thesis) -> dict[str, object]:
    """Idempotently store a provenance-bearing thesis under the caller's identity."""
    return get_service().theses_write(agent_name=agent_name, thesis=thesis)


@mcp.tool()
def lessons_write(agent_name: str, lesson: Lesson) -> dict[str, object]:
    """Store one ticker-anonymous structured PDCA lesson."""
    return get_service().lessons_write(agent_name=agent_name, lesson=lesson)


@mcp.tool()
def audit_reports_write(agent_name: str, report: AuditReport) -> dict[str, object]:
    """Store a structured discipline report without changing plans or orders."""
    return get_service().audit_reports_write(agent_name=agent_name, report=report)


@mcp.tool()
def proposal_write(agent_name: str, proposal: EvolutionProposal) -> dict[str, object]:
    """Store a PDCA proposal permanently constrained to non-production draft status."""
    return get_service().proposal_write(agent_name=agent_name, proposal=proposal)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
