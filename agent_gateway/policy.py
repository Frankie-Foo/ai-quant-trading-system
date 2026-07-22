"""Server-side least-privilege policy for every exposed agent tool."""

from __future__ import annotations

import os

from agent_gateway.contracts import AgentRole

ROLE_TOOLS: dict[AgentRole, frozenset[str]] = {
    AgentRole.COMMANDER: frozenset(
        {"sizing_preview", "exits_preview", "tradeplan_submit", "postgres_query", "theses_write"}
    ),
    AgentRole.RISK: frozenset(
        {
            "universe_query",
            "features_momentum",
            "features_liquidity",
            "features_order_flow",
            "features_short_flow",
            "features_sentiment",
            "postgres_query",
            "theses_write",
        }
    ),
    AgentRole.FACTOR_HUNTER: frozenset(
        {"features_momentum", "features_liquidity", "postgres_query", "theses_write"}
    ),
    AgentRole.ORDER_FLOW: frozenset({"features_order_flow", "postgres_query", "theses_write"}),
    AgentRole.SHORT_THESIS: frozenset({"features_short_flow", "postgres_query", "theses_write"}),
    AgentRole.SENTIMENT: frozenset({"features_sentiment", "postgres_query", "theses_write"}),
    AgentRole.DISCIPLINE: frozenset({"postgres_query", "audit_reports_write"}),
    AgentRole.PDCA: frozenset(
        {"postgres_query", "theses_write", "lessons_write", "proposal_write"}
    ),
}


class AuthorizationError(PermissionError):
    """Raised when a caller crosses the configured role boundary."""


def authorize(agent_name: str, tool: str) -> AgentRole:
    try:
        actor = AgentRole(agent_name.strip())
    except ValueError as exc:
        raise AuthorizationError("unknown agent role") from exc
    bound_name = os.getenv("QUANT_AGENT_NAME", "").strip()
    if bound_name and bound_name != actor.value:
        raise AuthorizationError("agent identity does not match this server instance")
    if tool not in ROLE_TOOLS[actor]:
        raise AuthorizationError(f"agent {actor.value!r} is not allowed to call {tool!r}")
    return actor
