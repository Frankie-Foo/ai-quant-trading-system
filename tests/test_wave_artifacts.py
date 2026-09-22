import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from operations import wave_artifacts


def _seed(root: Path) -> None:
    first = {
        "schema_version": "modern_funnel.first_wave.v3", "trade_date": "2026-09-22",
        "generated_at_utc": "2026-09-22T12:30:00Z", "candidates": [],
    }
    path = root / "first_wave_pool.json"
    path.write_text(json.dumps(first), encoding="utf-8")
    second = {
        **first, "schema_version": "modern_funnel.second_wave.v2",
        "generated_at_utc": "2026-09-22T13:00:00Z",
        "prior_wave_hashes": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()},
    }
    (root / "second_wave_pool.json").write_text(json.dumps(second), encoding="utf-8")


@pytest.mark.parametrize("fault", [None, "date", "naive", "future", "hash", "order", "missing"])
def test_wave_chain_validates_dates_times_and_exact_parent_bytes(
    tmp_path: Path, fault: str | None,
) -> None:
    _seed(tmp_path)
    path = tmp_path / "second_wave_pool.json"
    value = json.loads(path.read_text())
    if fault == "date":
        value["trade_date"] = "2026-09-21"
    elif fault == "naive":
        value["generated_at_utc"] = "2026-09-22T13:00:00"
    elif fault == "future":
        value["generated_at_utc"] = "2026-09-22T13:40:00Z"
    elif fault == "hash":
        parent = tmp_path / "first_wave_pool.json"
        parent.write_bytes(parent.read_bytes() + b" ")
    elif fault == "order":
        value["generated_at_utc"] = "2026-09-22T12:29:00Z"
    elif fault == "missing":
        value.pop("prior_wave_hashes")
    path.write_text(json.dumps(value), encoding="utf-8")
    day = date(2026, 9, 22)
    as_of = datetime(2026, 9, 22, 13, 35, tzinfo=UTC)
    if fault:
        with pytest.raises(ValueError):
            wave_artifacts.read_wave_artifact(path, trade_date=day, as_of=as_of)
    else:
        payload, digest = wave_artifacts.read_wave_artifact(path, trade_date=day, as_of=as_of)
        assert payload == value
        assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
