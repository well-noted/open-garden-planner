"""Measurement gating (design section 2.4). Small on purpose; reviewed as a security surface.

Three independent rejections, all reported as a short reason string so the
caller can keep the sample flagged ``gated`` for diagnostics:

1. input validity (non-finite, accuracy out of range, time not increasing),
2. Mahalanobis distance of the innovation (chi-square, 2 dof, 99 %),
3. implied-speed jump, which catches a spike that a large R lets through (2).
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np

from .types import (
    ACC_MAX_M,
    DT_MAX_S,
    DT_MIN_S,
    MAHALANOBIS_GATE,
    MAX_SPEED_MPS,
    REJECT_WINDOW_S,
    REJECTS_TO_RESEED,
    GnssSample,
)


def validate_gnss(sample: GnssSample, last_t: float | None) -> str | None:
    """Return a reject reason, or ``None`` when the sample is structurally usable."""
    vals = (sample.t, sample.lat, sample.lon, sample.accuracy_m)
    if not all(isinstance(v, int | float) and math.isfinite(v) for v in vals):
        return "non_finite"
    if not (0.0 < sample.accuracy_m <= ACC_MAX_M):
        return "accuracy_out_of_range"
    if abs(sample.lat) > 90.0 or abs(sample.lon) > 180.0:
        return "bad_coordinates"
    if last_t is not None and sample.t <= last_t:
        return "time_not_increasing"
    return None


def mahalanobis_d2(y: np.ndarray, s: np.ndarray) -> float:
    """Squared Mahalanobis distance ``y^T S^-1 y``."""
    return float(y @ np.linalg.solve(s, y))


def gate_measurement(
    y: np.ndarray, s: np.ndarray, *, jump_m: float, r: float, dt: float
) -> str | None:
    """``"mahalanobis"`` / ``"velocity_jump"`` when the measurement must be rejected.

    ``jump_m`` is the distance from the last filtered position to the new
    measurement. One measurement sigma ``r`` is allowed off it, otherwise honest
    5 m jitter would read as a 5 m/s jump (a documented deviation from the bare
    "implied speed" wording of the design).
    """
    if mahalanobis_d2(y, s) > MAHALANOBIS_GATE:
        return "mahalanobis"
    implied = max(0.0, jump_m - r) / min(max(dt, DT_MIN_S), DT_MAX_S)
    if implied > MAX_SPEED_MPS:
        return "velocity_jump"
    return None


class RejectTracker:
    """Three consecutive gate rejects within 5 s mean the *filter* is wrong: re-seed."""

    def __init__(self) -> None:
        self._times: deque[float] = deque(maxlen=REJECTS_TO_RESEED)

    def reject(self, t: float) -> bool:
        """Record a reject; ``True`` when the caller should re-seed from this sample."""
        self._times.append(t)
        if len(self._times) == REJECTS_TO_RESEED and t - self._times[0] <= REJECT_WINDOW_S:
            self._times.clear()
            return True
        return False

    def accept(self) -> None:
        self._times.clear()
