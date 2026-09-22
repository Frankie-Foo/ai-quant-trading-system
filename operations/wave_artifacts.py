"""Read a dated funnel chain, binding each child to its exact parent bytes."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_STAGES = {
    "first_wave_pool.json": ("first_wave", ()),
    "second_wave_pool.json": ("second_wave", ("first_wave_pool.json",)),
    "final_wave_pool.json": ("final_rank", ("first_wave_pool.json", "second_wave_pool.json")),
    "open_decision.json": ("open_decision", ("final_wave_pool.json",)),
}


def read_wave_artifact(
    path: Path, *, trade_date: date, as_of: datetime,
) -> tuple[dict[str, Any], str]:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("wave decision timestamp must be aware")
    stage, parent_names = _STAGES[path.name]
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("wave artifact must be an object")
    if (payload.get("trade_date") != trade_date.isoformat()
            or not str(payload.get("schema_version", "")).startswith(f"modern_funnel.{stage}.v")):
        raise ValueError("wave date or schema mismatch")
    generated = datetime.fromisoformat(str(payload.get("generated_at_utc", "")))
    if (generated.tzinfo is None or generated.utcoffset() is None
            or generated > as_of
            or generated.astimezone(ZoneInfo("America/New_York")).date() != trade_date):
        raise ValueError("wave generation timestamp is invalid")
    hashes = payload.get("prior_wave_hashes", {})
    if not isinstance(hashes, dict) or set(hashes) != set(parent_names):
        raise ValueError("wave parent hashes are missing or unexpected")
    for name in parent_names:
        _, digest = read_wave_artifact(path.parent / name, trade_date=trade_date, as_of=generated)
        if hashes[name] != digest:
            raise ValueError("wave parent hash mismatch")
    return payload, hashlib.sha256(raw).hexdigest()
