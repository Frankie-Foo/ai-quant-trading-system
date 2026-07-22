"""Point-in-time meta-label gate."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class MetaDecision:
    probability: float
    threshold: float
    pass_gate: bool
    model_provenance: str


def gate(
    probability: float,
    *,
    threshold: float,
    calibrated: bool,
    model_provenance: str,
) -> MetaDecision:
    """Admit a model probability only after explicit OOS calibration approval."""
    if not calibrated:
        raise ValueError("meta probability is unavailable until the model is calibrated")
    if not model_provenance.strip():
        raise ValueError("model provenance is required")
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("probability must be finite and in [0, 1]")
    if not math.isfinite(threshold) or not 0 < threshold < 1:
        raise ValueError("threshold must be finite and in (0, 1)")
    return MetaDecision(
        probability=probability,
        threshold=threshold,
        pass_gate=probability >= threshold,
        model_provenance=model_provenance,
    )
