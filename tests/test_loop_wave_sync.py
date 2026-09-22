import json
from datetime import date
from pathlib import Path

import pytest

from scripts.sync_loop_daily_review import _intraday_waves


def test_intraday_waves_preserve_frozen_rank_facts(tmp_path: Path) -> None:
    trade_date = date(2026, 9, 16)
    day = tmp_path / trade_date.isoformat()
    day.mkdir()
    files = (
        ("first_wave_pool.json", "AAA", 1),
        ("second_wave_pool.json", "AAA", 2),
        ("final_wave_pool.json", "AAA", 3),
    )
    for filename, symbol, repeats in files:
        (day / filename).write_text(
            json.dumps({
                "trade_date": trade_date.isoformat(),
                "generated_at_utc": "2026-09-16T12:30:00+00:00",
                "source_snapshot_id": "snapshot-1",
                "candidates": [{
                    "symbol": symbol,
                    "wave_rank": 1,
                    "repeat_count": repeats,
                    "weighted_score": 9.0,
                }],
            }),
            encoding="utf-8",
        )

    waves = _intraday_waves(tmp_path, trade_date)

    assert waves["status"] == "available"
    assert waves["missing_stages"] == []
    items = waves["waves"]
    assert isinstance(items, list)
    first = items[0]
    assert first["stage"] == "08:30_top20"
    assert first["candidates"] == [{
        "symbol": "AAA", "wave_rank": 1, "repeat_count": 1, "weighted_score": 9.0,
    }]
    assert len(first["content_sha256"]) == 64


def test_intraday_waves_reject_wrong_trade_date(tmp_path: Path) -> None:
    day = tmp_path / "2026-09-16"
    day.mkdir()
    (day / "first_wave_pool.json").write_text(
        json.dumps({"trade_date": "2026-09-15", "candidates": []}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="intraday wave is invalid"):
        _intraday_waves(tmp_path, date(2026, 9, 16))
