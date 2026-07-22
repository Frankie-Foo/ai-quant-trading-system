from __future__ import annotations

from datetime import date
from pathlib import Path

from schedule.research_cycle import _stages, research_window


def test_research_window_has_feature_warmup_and_month_aligned_news() -> None:
    window = research_window(date(2026, 7, 16), sessions=252)
    assert window.end == date(2026, 7, 16)
    assert window.daily_start < window.news_start < window.end
    assert window.news_start.day == 1
    assert window.news_end_exclusive == date(2026, 7, 17)


def test_research_cycle_ends_in_governed_evolution_and_evidence(
    tmp_path: Path,
) -> None:
    window = research_window(date(2026, 7, 16), sessions=252)
    stages = _stages(window, data_root=tmp_path / "data", state_root=tmp_path / "state")
    names = tuple(stage[0] for stage in stages)
    assert names[-2:] == ("sandbox_evolution", "maturity_evidence")
    assert "net_labels_oos" in names
