"""Generate versioned JSON Schemas for integration consumers."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from .liquidations import LiquidationEvent
from .models import (
    OutcomeRecord,
    PositionRecommendation,
    RiskSnapshot,
)


def write_schemas(output_directory: Path) -> tuple[Path, ...]:
    destination = output_directory.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    contracts: dict[str, type[BaseModel]] = {
        "risk-snapshot-v1.schema.json": RiskSnapshot,
        "position-recommendation-v1.schema.json": PositionRecommendation,
        "liquidation-event-v1.schema.json": LiquidationEvent,
        "outcome-v1.schema.json": OutcomeRecord,
    }
    paths: list[Path] = []
    for filename, model in contracts.items():
        path = destination / filename
        path.write_text(
            json.dumps(
                model.model_json_schema(),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        paths.append(path)
    return tuple(paths)
