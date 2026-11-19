"""Anchor calibration (design section 2.6): raw device ENU -> plan ENU.

* 1 point: translation only ("calibrate here"). Session state, never persisted.
* 2-4 points: similarity (affine residual diagnostic with ``allow_affine`` and >= 3
  points) through :func:`open_garden_planner.core.georef.fit_from_reference_points`.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from ..georef import FitResult, GeoReference, GeorefError, ReferencePoint, fit_from_reference_points
from .survey import SurveyResult

MAX_RMS_M = 3.0
MAX_RESIDUAL_FACTOR = 2.5
RESIDUAL_FLOOR_M = 0.05  # keeps "2.5 x median" meaningful when the median is ~0
MIN_POINTS, MAX_POINTS = 2, 4


class CalibrationError(ValueError):
    """The calibration points are unusable (degenerate, inconsistent or mis-tapped)."""


@dataclass(frozen=True)
class Anchor:
    """The device-to-plan correction in force; ``apply`` maps raw ENU metres to plan ENU metres."""

    kind: str  # "gnss_point" | "plan_points"
    t: float
    dx: float = 0.0
    dy: float = 0.0
    fit: GeoReference | None = None
    base: GeoReference | None = None
    fit_rms_m: float | None = None
    n_points: int = 1

    def apply(self, e: float, n: float) -> tuple[float, float]:
        if self.fit is None or self.base is None:
            return e + self.dx, n + self.dy
        # raw ENU -> pseudo lat/lon (base frame) -> canvas via the fitted georeference ->
        # back to the base frame's ENU, so the result lives in the same frame as the plan.
        lat, lon = self.base.enu_to_gps(e, n)
        x, y = self.fit.gps_to_canvas(lat, lon)
        return self.base.gps_to_enu(*self.base.canvas_to_gps(x, y))


def _plan_enu(georef: GeoReference, x_cm: float, y_cm: float) -> tuple[float, float]:
    return georef.gps_to_enu(*georef.canvas_to_gps(x_cm, y_cm))


def calibrate_one_point(
    plan_xy: tuple[float, float], survey: SurveyResult, georef: GeoReference, t: float
) -> Anchor:
    """Translation ``t = p - g`` between the known plan point and the surveyed position."""
    if not math.isfinite(t):
        raise CalibrationError("time must be finite")
    try:
        pe, pn = _plan_enu(georef, *plan_xy)
    except GeorefError as exc:
        raise CalibrationError(str(exc)) from exc
    return Anchor("gnss_point", t, dx=pe - survey.e_m, dy=pn - survey.n_m)


def calibrate_points(
    pairs: Sequence[tuple[tuple[float, float], SurveyResult]],
    georef: GeoReference,
    t: float,
    *,
    allow_affine: bool = False,
) -> tuple[Anchor, FitResult]:
    """2-4 (plan point in scene units, surveyed position) pairs -> an :class:`Anchor`.

    Refuses when the fit rms exceeds 3 m or one residual exceeds 2.5 x the median
    (a mis-tapped point).
    """
    if not (MIN_POINTS <= len(pairs) <= MAX_POINTS):
        raise CalibrationError(f"need {MIN_POINTS} to {MAX_POINTS} calibration points")
    try:
        pts = []
        for i, ((x, y), sv) in enumerate(pairs):
            lat, lon = georef.enu_to_gps(sv.e_m, sv.n_m)
            pts.append(ReferencePoint(f"c{i}", lat, lon, x, y))
        fit = fit_from_reference_points(
            pts,
            scale_meters_per_unit=georef.scale_meters_per_unit,
            rotation_deg=georef.rotation_deg,
            allow_affine=allow_affine,
            source="anchor",
        )
    except GeorefError as exc:
        raise CalibrationError(str(exc)) from exc
    if fit.rms_m > MAX_RMS_M:
        raise CalibrationError(f"calibration residual rms {fit.rms_m:.1f} m is too large")
    if len(pairs) >= 3:
        med = max(statistics.median(fit.residuals_m), RESIDUAL_FLOOR_M)
        if max(fit.residuals_m) > MAX_RESIDUAL_FACTOR * med:
            raise CalibrationError("one calibration point disagrees with the others (mis-tapped?)")
    return Anchor("plan_points", t, fit=fit.georef, base=georef, fit_rms_m=fit.rms_m, n_points=len(pairs)), fit
