"""Anchor calibration (design section 2.6): raw device ENU -> plan ENU.

* 1 point: translation only ("calibrate here"). Session state, never persisted.
* 2-4 points: a 2-D similarity (rotation, uniform scale, translation) from the raw ENU
  metres to the plan's ENU metres (``GeoFrame``), closed-form (Umeyama in complex form).
  With ``allow_affine`` and >= 3 points the reported residuals are those of a full affine
  fit, a diagnostic of shear or anisotropy; the correction applied is still the similarity.

The anchor is a session correction of the *phone*; it never touches the plan's georeference
(``core/georeference.py`` stays the record).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from ..geo_frame import GeoFrame, GeoreferenceError
from .survey import SurveyResult

MAX_RMS_M = 3.0
MAX_RESIDUAL_FACTOR = 2.5
RESIDUAL_FLOOR_M = 0.05  # keeps "2.5 x median" meaningful when the median is ~0
MIN_POINTS, MAX_POINTS = 2, 4
MIN_SPREAD_M = 1e-3  # points closer together than this (RMS spread) coincide
COLLINEAR_RATIO = 1e-3  # minor/major principal std-dev ratio below this is a line


class CalibrationError(ValueError):
    """The calibration points are unusable (degenerate, inconsistent or mis-tapped)."""


@dataclass(frozen=True)
class FitResult:
    """Outcome of a calibration fit (metres)."""

    rms_m: float
    residuals_m: tuple[float, ...]
    scale_factor: float  # plan metres per raw metre
    method: str  # "similarity" | "affine"


@dataclass(frozen=True)
class Anchor:
    """The device-to-plan correction in force; ``apply`` maps raw ENU metres to plan ENU metres."""

    kind: str  # "gnss_point" | "plan_points"
    t: float
    dx: float = 0.0
    dy: float = 0.0
    #: plan = rot * (e + i n) + shift as complex numbers (``rot`` = scale * e^{i phi}); None = translation.
    rot: complex | None = None
    shift: complex = 0j
    fit_rms_m: float | None = None
    n_points: int = 1

    def apply(self, e: float, n: float) -> tuple[float, float]:
        if self.rot is None:
            return e + self.dx, n + self.dy
        z = self.rot * complex(e, n) + self.shift
        return z.real, z.imag


def _plan_enu(georef: GeoFrame, x_cm: float, y_cm: float) -> tuple[float, float]:
    return georef.gps_to_enu(*georef.canvas_to_gps(x_cm, y_cm))


def _require_not_collinear(pts: Sequence[tuple[float, float]], what: str) -> None:
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    syy = sum((p[1] - my) ** 2 for p in pts)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    half_tr = (sxx + syy) / 2
    root = math.sqrt(max(half_tr * half_tr - (sxx * syy - sxy * sxy), 0.0))
    big, small = half_tr + root, max(half_tr - root, 0.0)
    if big <= 0 or math.sqrt(small / big) < COLLINEAR_RATIO:
        raise CalibrationError(f"{what} calibration points are collinear")


def _fit_similarity(
    raw: Sequence[tuple[float, float]], plan: Sequence[tuple[float, float]]
) -> tuple[complex, complex]:
    """``(rot, shift)`` with ``plan ~= rot * raw + shift`` (complex-number Umeyama)."""
    n = len(raw)
    zr = [complex(*p) for p in raw]
    zp = [complex(*p) for p in plan]
    cr = sum(zr) / n
    cp = sum(zp) / n
    spread_r = math.sqrt(sum(abs(z - cr) ** 2 for z in zr) / n)
    spread_p = math.sqrt(sum(abs(z - cp) ** 2 for z in zp) / n)
    if spread_r < MIN_SPREAD_M or spread_p < MIN_SPREAD_M:
        raise CalibrationError("calibration points coincide: they must be spread apart")
    if n >= 3:
        _require_not_collinear(raw, "device")
        _require_not_collinear(plan, "plan")
    num = sum((p - cp) * (r - cr).conjugate() for r, p in zip(zr, zp, strict=True))
    den = sum(abs(r - cr) ** 2 for r in zr)
    if abs(num) < 1e-12 or den < 1e-12:
        raise CalibrationError("calibration points are degenerate")
    rot = num / den
    return rot, cp - rot * cr


def _solve3(a: list[list[float]], b: list[float]) -> list[float]:
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise CalibrationError("calibration points are degenerate")
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(3):
            if r != col:
                f = m[r][col] / m[col][col]
                for c in range(col, 4):
                    m[r][c] -= f * m[col][c]
    return [m[i][3] / m[i][i] for i in range(3)]


def _affine_residuals(
    raw: Sequence[tuple[float, float]], plan: Sequence[tuple[float, float]]
) -> list[float]:
    """Residuals (metres) of the full least-squares affine raw ENU -> plan ENU."""
    s = [[0.0] * 3 for _ in range(3)]
    bx = [0.0] * 3
    by = [0.0] * 3
    for (e, nn), (x, y) in zip(raw, plan, strict=True):
        row = (e, nn, 1.0)
        for i in range(3):
            for j in range(3):
                s[i][j] += row[i] * row[j]
            bx[i] += row[i] * x
            by[i] += row[i] * y
    mx = _solve3(s, bx)
    my = _solve3(s, by)
    return [
        math.hypot(mx[0] * e + mx[1] * nn + mx[2] - x, my[0] * e + my[1] * nn + my[2] - y)
        for (e, nn), (x, y) in zip(raw, plan, strict=True)
    ]


def calibrate_one_point(
    plan_xy: tuple[float, float], survey: SurveyResult, georef: GeoFrame, t: float
) -> Anchor:
    """Translation ``t = p - g`` between the known plan point and the surveyed position."""
    if not math.isfinite(t):
        raise CalibrationError("time must be finite")
    try:
        pe, pn = _plan_enu(georef, *plan_xy)
    except GeoreferenceError as exc:
        raise CalibrationError(str(exc)) from exc
    return Anchor("gnss_point", t, dx=pe - survey.e_m, dy=pn - survey.n_m)


def calibrate_points(
    pairs: Sequence[tuple[tuple[float, float], SurveyResult]],
    georef: GeoFrame,
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
        plan = [_plan_enu(georef, x, y) for (x, y), _sv in pairs]
    except GeoreferenceError as exc:
        raise CalibrationError(str(exc)) from exc
    raw = [(sv.e_m, sv.n_m) for _xy, sv in pairs]
    rot, shift = _fit_similarity(raw, plan)
    residuals = [
        abs(rot * complex(*r) + shift - complex(*p)) for r, p in zip(raw, plan, strict=True)
    ]
    method = "similarity"
    if allow_affine and len(pairs) >= 3:
        method = "affine"
        residuals = _affine_residuals(raw, plan)
    rms = math.sqrt(sum(r * r for r in residuals) / len(residuals))
    fit = FitResult(rms, tuple(residuals), abs(rot), method)
    if fit.rms_m > MAX_RMS_M:
        raise CalibrationError(f"calibration residual rms {fit.rms_m:.1f} m is too large")
    if len(pairs) >= 3:
        med = max(statistics.median(fit.residuals_m), RESIDUAL_FLOOR_M)
        if max(fit.residuals_m) > MAX_RESIDUAL_FACTOR * med:
            raise CalibrationError("one calibration point disagrees with the others (mis-tapped?)")
    return Anchor("plan_points", t, rot=rot, shift=shift, fit_rms_m=fit.rms_m, n_points=len(pairs)), fit
