from __future__ import annotations

import pandas as pd

from scripts.run_noise_area_long_only import _metrics


def test_payoff_metrics_allow_lower_win_rate() -> None:
    trades = pd.DataFrame({"net_pnl": [300.0, 250.0, -100.0, -100.0, -100.0]})

    metrics = _metrics(trades)

    assert metrics["win_rate"] == 0.4
    assert metrics["average_win_loss"] == 2.75
    assert metrics["profit_factor"] > 1.0
