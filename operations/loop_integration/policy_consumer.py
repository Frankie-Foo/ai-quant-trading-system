from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kernel.strategy_policy import (
    ALLOWED_PARAMETER,
    StrategyPolicy,
    build_strategy_policy,
    load_strategy_policy,
    write_strategy_policy,
)

from .contracts import LoopPolicyCandidate


def _min_rvol(payload: dict[str, Any]) -> float:
    selection = payload.get("selection_policy")
    if not isinstance(selection, dict):
        raise ValueError("Loop selection_policy must be an object")
    overrides = selection.get("parameter_overrides")
    if isinstance(overrides, dict) and set(overrides) == {ALLOWED_PARAMETER}:
        return float(overrides[ALLOWED_PARAMETER])
    universe = selection.get("universe")
    if isinstance(universe, dict) and set(universe) == {"min_rvol"}:
        return float(universe["min_rvol"])
    raise ValueError("Loop candidate contains unsupported or missing selection parameters")


def install_shadow_candidate(
    candidate: LoopPolicyCandidate,
    *,
    active_path: Path,
    challenger_path: Path,
    installed_at_utc: datetime | None = None,
) -> StrategyPolicy:
    active = load_strategy_policy(active_path, required_status="active")
    trading_policy = candidate.payload.get("trading_policy")
    if trading_policy not in ({}, None):
        raise ValueError("Loop candidate attempted to modify local trading policy")
    min_rvol = _min_rvol(candidate.payload)
    revision = str(candidate.payload["strategy_revision_id"])
    fingerprint = str(candidate.payload["strategy_fingerprint"])
    revision_slug = re.sub(r"[^a-z0-9._-]+", "-", revision.lower()).strip("-.")
    if not revision_slug:
        raise ValueError("Loop strategy revision cannot form a local policy version")
    proposed = build_strategy_policy(
        version=f"challenger-loop-{revision_slug[:40]}",
        status="shadow",
        min_rvol=min_rvol,
        created_at_utc=installed_at_utc or datetime.now(UTC),
        previous_version=active.version,
        source_snapshot_ids=(candidate.id, revision, fingerprint),
    )
    if challenger_path.exists():
        existing = load_strategy_policy(challenger_path, required_status="shadow")
        if existing == proposed:
            return existing
        raise RuntimeError("a different shadow challenger is already installed")
    write_strategy_policy(challenger_path, proposed)
    return proposed
