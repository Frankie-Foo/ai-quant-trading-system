"""Load the latest accepted point-in-time selection for one XNYS session."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator

from data_plane.contracts import DatasetSnapshot


class LockedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(min_length=1, max_length=16)
    selection_rank: int = Field(gt=0)
    rvol: float = Field(gt=0)
    price: float = Field(gt=0)
    adv_usd: float = Field(gt=0)
    atr_pct: float = Field(gt=0)
    tier: str = Field(pattern=r"^(mega|large|mid|small)$")
    event_count: int = Field(default=0, ge=0)
    catalyst_categories: tuple[str, ...] = ()
    market_cap: float | None = Field(default=None, gt=0)
    premarket_close: float | None = Field(default=None, gt=0)
    premarket_vwap: float | None = Field(default=None, gt=0)
    premarket_return: float | None = None
    premarket_gap_return: float | None = None
    premarket_above_vwap: bool | None = None
    current_halt: bool = False
    recent_luld_count: int = Field(default=0, ge=0)
    luld_risk: bool = False

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


@dataclass(frozen=True)
class LockedSelection:
    trade_date: date
    snapshot: DatasetSnapshot
    candidates: tuple[LockedCandidate, ...]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(candidate.symbol for candidate in self.candidates)


def _manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"selection manifest is not an object: {path}")
    return value


def load_locked_selection(
    data_root: str | Path,
    trade_date: date,
    *,
    min_rvol: float,
) -> LockedSelection:
    root = Path(data_root)
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (root / "accepted").glob(
        "kernel.universe.selection_gates-*/data.parquet"
    ):
        dates = pl.read_parquet(path, columns=["session_date"]).get_column(
            "session_date"
        ).unique()
        if dates.to_list() != [trade_date]:
            continue
        snapshot = DatasetSnapshot.model_validate(_manifest(path.parent / "manifest.json"))
        snapshot.assert_usable()
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        raise FileNotFoundError(f"no accepted locked selection for {trade_date}")
    _, path, snapshot = max(matches, key=lambda item: item[0])
    if snapshot.schema_version != "selection_gates.v2":
        raise ValueError(
            "locked selection uses an obsolete schema without directional volume"
        )
    required_columns = [
        "symbol",
        "session_date",
        "selection_rank",
        "pass_gate",
        "rvol",
        "directional_volume_confirmed",
        "price",
        "adv_usd",
        "atr_pct",
        "tier",
    ]
    optional_columns = [
        "event_count",
        "catalyst_categories",
        "market_cap",
        "premarket_close",
        "premarket_vwap",
        "premarket_return",
        "premarket_gap_return",
        "premarket_above_vwap",
        "current_halt",
        "recent_luld_count",
        "luld_risk",
    ]
    available_columns = pl.read_parquet(path, n_rows=0).columns
    frame = pl.read_parquet(
        path,
        columns=[
            column
            for column in (*required_columns, *optional_columns)
            if column in available_columns
        ],
    )
    survivors = frame.filter(pl.col("pass_gate")).sort("selection_rank", "symbol")
    if survivors.is_empty():
        raise ValueError("locked selection contains no gate survivors")
    duplicate_count = survivors.height - survivors.get_column("symbol").n_unique()
    if duplicate_count:
        raise ValueError("locked selection contains duplicate symbols")
    if survivors.filter(pl.col("rvol") <= min_rvol).height:
        raise ValueError("locked selection contains RVOL at or below the strict threshold")
    if survivors.filter(~pl.col("directional_volume_confirmed")).height:
        raise ValueError(
            "locked selection contains a survivor without directional volume confirmation"
        )
    candidates = tuple(
        LockedCandidate.model_validate(
            {
                "symbol": row["symbol"],
                "selection_rank": row["selection_rank"],
                "rvol": row["rvol"],
                "price": row["price"],
                "adv_usd": row["adv_usd"],
                "atr_pct": row["atr_pct"],
                "tier": row["tier"],
                **{
                    column: row[column]
                    for column in optional_columns
                    if column in row
                },
            }
        )
        for row in survivors.iter_rows(named=True)
    )
    return LockedSelection(trade_date=trade_date, snapshot=snapshot, candidates=candidates)
