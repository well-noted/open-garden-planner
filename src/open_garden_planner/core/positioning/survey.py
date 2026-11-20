"""Survey-In: stand still, average a few fixes (design section 2.5, corrected).

GNSS error at 1 Hz is strongly correlated over tens of seconds, so 3-5 samples
are close to ONE independent measurement. ``N_eff`` is therefore 1.2..1.5, not
``N/3``: averaging buys at most 10-20 %, and it removes jitter, not systematic
offset. The result never passes through the Kalman filter.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from ..geo_frame import GeoFrame, GeoreferenceError
from .gating import validate_gnss
from .types import GnssSample

MIN_SAMPLES = 4
MIN_DURATION_S = 3.0
HARD_STOP_S = 5.0
MIN_SIGMA_M = 0.5
N_EFF_MIN, N_EFF_MAX = 1.2, 1.5


class SurveyError(ValueError):
    """The samples cannot make a trustworthy stand-still estimate."""


@dataclass(frozen=True)
class SurveyResult:
    """A raw-frame (pre-anchor) position; ``sigma_m`` is a computed spread, not a validated accuracy."""

    e_m: float
    n_m: float
    lat: float
    lon: float
    sigma_m: float
    n_used: int
    n_discarded: int
    duration_s: float


def n_eff(n: int) -> float:
    """1.2 at N <= 4 rising linearly to 1.5 at N >= 8."""
    return min(N_EFF_MAX, max(N_EFF_MIN, N_EFF_MIN + (N_EFF_MAX - N_EFF_MIN) * (n - 4) / 4.0))


def survey_in(samples: Sequence[GnssSample], georef: GeoFrame) -> SurveyResult:
    """Weighted mean (``w = 1/max(accuracy, 1)^2``) of the usable samples, in ENU metres."""
    valid: list[GnssSample] = []
    last_t: float | None = None
    for s in samples:
        if validate_gnss(s, last_t) is None:
            valid.append(s)
            last_t = s.t
    if not valid:
        raise SurveyError("no usable samples")
    t0 = valid[0].t
    valid = [s for s in valid if s.t - t0 <= HARD_STOP_S]  # hard stop
    if len(valid) < MIN_SAMPLES:
        raise SurveyError(f"need at least {MIN_SAMPLES} samples, got {len(valid)}")
    duration = valid[-1].t - t0
    if duration < MIN_DURATION_S:
        raise SurveyError("stand still for at least 3 seconds")

    try:
        enu = [georef.gps_to_enu(s.lat, s.lon) for s in valid]
    except GeoreferenceError as exc:
        raise SurveyError(str(exc)) from exc

    # Abort if the user walked: any sample farther from the first than max(1.5 m, 2 x accuracy).
    for s, p in zip(valid, enu, strict=True):
        if math.dist(p, enu[0]) > max(1.5, 2.0 * max(s.accuracy_m, valid[0].accuracy_m)):
            raise SurveyError("moved during survey")

    accs = [s.accuracy_m for s in valid]
    limit = max(2.0 * statistics.median(accs), 1.5 * min(accs))
    kept = [(s, p) for s, p in zip(valid, enu, strict=True) if s.accuracy_m <= limit]
    if len(kept) < MIN_SAMPLES - 1:
        raise SurveyError("too few good samples after discarding the poor ones")

    w = [1.0 / max(s.accuracy_m, 1.0) ** 2 for s, _ in kept]
    wsum = sum(w)
    e = sum(wi * p[0] for wi, (_, p) in zip(w, kept, strict=True)) / wsum
    n = sum(wi * p[1] for wi, (_, p) in zip(w, kept, strict=True)) / wsum
    sigma = max(statistics.median(s.accuracy_m for s, _ in kept) / math.sqrt(n_eff(len(kept))), MIN_SIGMA_M)
    lat, lon = georef.enu_to_gps(e, n)
    return SurveyResult(
        e_m=e,
        n_m=n,
        lat=lat,
        lon=lon,
        sigma_m=sigma,
        n_used=len(kept),
        n_discarded=len(samples) - len(kept),
        duration_s=duration,
    )
