from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class PurgedFold:
    train_indices: NDArray[np.int64]
    validation_indices: NDArray[np.int64]
    validation_start: date
    validation_end: date
    embargo_end: date


def purged_walk_forward_splits(
    dates: NDArray[Any] | list[date],
    *,
    n_splits: int,
    purge_days: int,
    embargo_days: int,
) -> tuple[PurgedFold, ...]:
    """Create deterministic expanding OOS folds without random shuffling.

    The first chronological block is training-only. Every later block becomes one
    validation fold; training is restricted to dates strictly before the purge gap.
    ``embargo_end`` is recorded for subsequent model-comparison/test scheduling.
    """
    if n_splits < 2:
        raise ValueError("n_splits must be at least two")
    if purge_days < 0 or embargo_days < 0:
        raise ValueError("purge and embargo days must be nonnegative")
    values = np.asarray(dates, dtype=object)
    if values.ndim != 1 or values.size == 0:
        raise ValueError("dates must be a non-empty one-dimensional sequence")
    if not all(isinstance(value, date) for value in values):
        raise ValueError("every split value must be a date")
    if any(values[index] > values[index + 1] for index in range(values.size - 1)):
        raise ValueError("dates must be sorted chronologically")
    unique_dates = np.array(sorted(set(values.tolist())), dtype=object)
    blocks = np.array_split(unique_dates, n_splits + 1)
    if any(block.size == 0 for block in blocks):
        raise ValueError("not enough unique dates for the requested folds")

    folds: list[PurgedFold] = []
    for validation_dates in blocks[1:]:
        validation_start = validation_dates[0]
        validation_end = validation_dates[-1]
        if not isinstance(validation_start, date) or not isinstance(validation_end, date):
            raise AssertionError("date block contains an invalid value")
        train_cutoff = validation_start - timedelta(days=purge_days)
        train_indices = np.flatnonzero(
            np.array([value < train_cutoff for value in values], dtype=bool)
        ).astype(np.int64)
        validation_set = set(validation_dates.tolist())
        validation_indices = np.flatnonzero(
            np.array([value in validation_set for value in values], dtype=bool)
        ).astype(np.int64)
        if train_indices.size == 0 or validation_indices.size == 0:
            raise ValueError("a fold has no train or validation observations")
        folds.append(
            PurgedFold(
                train_indices=train_indices,
                validation_indices=validation_indices,
                validation_start=validation_start,
                validation_end=validation_end,
                embargo_end=validation_end + timedelta(days=embargo_days),
            )
        )
    return tuple(folds)
