from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from kernel.meta import gate
from research.validation import purged_walk_forward_splits


def test_meta_gate_rejects_uncalibrated_or_unprovenanced_probability() -> None:
    with pytest.raises(ValueError, match="calibrated"):
        gate(0.8, threshold=0.6, calibrated=False, model_provenance="model:v1")
    with pytest.raises(ValueError, match="provenance"):
        gate(0.8, threshold=0.6, calibrated=True, model_provenance="")
    decision = gate(
        0.8,
        threshold=0.6,
        calibrated=True,
        model_provenance="lightgbm:v1|test_window=2026H1",
    )
    assert decision.pass_gate is True


def test_purged_walk_forward_never_trains_on_validation_or_embargo_dates() -> None:
    start = date(2025, 1, 1)
    dates = np.array([start + timedelta(days=index) for index in range(100)])
    folds = purged_walk_forward_splits(dates, n_splits=4, purge_days=1, embargo_days=2)
    assert len(folds) == 4
    for fold in folds:
        train_dates = dates[fold.train_indices]
        validation_dates = dates[fold.validation_indices]
        assert train_dates.max() < validation_dates.min() - timedelta(days=1)
        assert not set(train_dates).intersection(validation_dates)
        assert fold.embargo_end >= validation_dates.max() + timedelta(days=2)


def test_purged_walk_forward_is_deterministic_and_order_preserving() -> None:
    dates = np.array([date(2025, 1, 1) + timedelta(days=index) for index in range(60)])
    first = purged_walk_forward_splits(dates, n_splits=3, purge_days=1, embargo_days=2)
    second = purged_walk_forward_splits(dates, n_splits=3, purge_days=1, embargo_days=2)
    for left, right in zip(first, second, strict=True):
        assert np.array_equal(left.train_indices, right.train_indices)
        assert np.array_equal(left.validation_indices, right.validation_indices)
        assert np.all(np.diff(left.train_indices) > 0)
        assert np.all(np.diff(left.validation_indices) > 0)
