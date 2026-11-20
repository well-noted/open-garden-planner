"""Qt-free plan georeferencing: WGS84 (lat, lon) <-> CAD scene (x_cm, y_cm).

The plan stays a metric CAD drawing; this module is the bridge that says where on
Earth a scene point lies and back. It is *plan metadata only* - no object coordinate
is ever rewritten - and it is an additive key (``metadata.georeference``) in the
``.ogp`` file, so ``FILE_VERSION`` does not move.

Frames (ADR-002, ADR-037 addendum, §8.20):

- Scene coordinates are centimetres in the CAD Y-up frame (+y is plan-up).
- ``north_angle_deg`` is the direction of TRUE NORTH on the plan, clockwise from
  plan-up (0 = north up, 90 = north to the right) - the plan's own setting, passed in
  by the caller. With it the east and north unit vectors in the scene are
  ``(cos a, -sin a)`` and ``(sin a, cos a)``; at ``a = 0`` that is +x = East, +y = North.
- ``rotation_deg`` is an extra turn of the same kind owned by the georeference itself
  (a satellite bound while the plan's north was turned stores the compensation here).
- ``fit_north_angle_deg`` is the plan's north angle at the moment a calibration was
  fitted. A calibrated georeference LOCKS to it (ADR-079): the control points are
  ground truth, so a later ``SetPlanNorthCommand`` cannot skew the affine residual.
  An uncalibrated one has no lock and follows the plan's north.

Projection: a local tangent plane at ``origin_lat`` using the WGS84 meridional and
prime-vertical radii of curvature, sub-millimetre accurate over property scales.
Ground-control points (:func:`fit_affine_calibration`) add a residual 2-D affine
correction in scene cm on top of that base projection.

Standard library only; every numeric input rejects booleans, ``NaN`` and ``Inf``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# WGS84 ellipsoid constants
_WGS84_A_M = 6378137.0
_WGS84_F = 1.0 / 298.257223563
_WGS84_E2 = 2.0 * _WGS84_F - _WGS84_F * _WGS84_F

#: Beyond this latitude a degree of longitude is too short for the local tangent
#: plane to invert meaningfully (the poles themselves are singular).
MAX_ORIGIN_LAT_DEG = 89.9
#: Most ground-control points a georeference keeps (a hostile file must not grow one).
MAX_CONTROL_POINTS = 64
#: The affine residual must stay a plausible map correction, not a degenerate one.
_MIN_AFFINE_DET = 1e-6


class GeoreferenceError(ValueError):
    """Raised when georeference data or calibration inputs are invalid."""


def _require_finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GeoreferenceError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError as error:  # a 400-digit integer in a hostile file
        raise GeoreferenceError(f"{name} must be finite") from error
    if not math.isfinite(result):
        raise GeoreferenceError(f"{name} must be finite")
    return result


def _require_lat_lon(lat: Any, lon: Any) -> tuple[float, float]:
    lat_f = _require_finite(lat, "lat")
    lon_f = _require_finite(lon, "lon")
    if not -90.0 <= lat_f <= 90.0:
        raise GeoreferenceError(f"lat out of range [-90, 90]: {lat_f}")
    if not -180.0 <= lon_f <= 180.0:
        raise GeoreferenceError(f"lon out of range [-180, 180]: {lon_f}")
    return lat_f, lon_f


def _finite_pair(first: float, second: float, what: str) -> tuple[float, float]:
    """An output pair, or an error: huge finite inputs overflow to ``inf``/``nan``."""
    if not (math.isfinite(first) and math.isfinite(second)):
        raise GeoreferenceError(f"{what} is not finite (input out of range)")
    return first, second


def meters_per_degree(lat_deg: float) -> tuple[float, float]:
    """Return ``(meters_per_deg_lat, meters_per_deg_lon)`` on WGS84 at ``lat_deg``."""
    phi = math.radians(lat_deg)
    sin_phi = math.sin(phi)
    cos_phi = math.cos(phi)
    denom = math.sqrt(1.0 - _WGS84_E2 * sin_phi * sin_phi)
    meridional_r = _WGS84_A_M * (1.0 - _WGS84_E2) / (denom * denom * denom)
    prime_vertical_r = _WGS84_A_M / denom
    rad = math.pi / 180.0
    return meridional_r * rad, prime_vertical_r * cos_phi * rad


@dataclass(frozen=True)
class ControlPoint:
    """A matched ground-control pair between WGS84 GPS and CAD scene cm."""

    lat: float
    lon: float
    x_cm: float
    y_cm: float
    label: str = ""

    def __post_init__(self) -> None:
        _require_lat_lon(self.lat, self.lon)
        _require_finite(self.x_cm, "x_cm")
        _require_finite(self.y_cm, "y_cm")
        if not isinstance(self.label, str):
            raise GeoreferenceError("label must be a string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "x_cm": self.x_cm,
            "y_cm": self.y_cm,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ControlPoint:
        if not isinstance(data, Mapping):
            raise GeoreferenceError("control point must be an object")
        lat, lon = _require_lat_lon(data.get("lat"), data.get("lon"))
        label = data.get("label", "")
        if not isinstance(label, str):
            raise GeoreferenceError("label must be a string")
        return cls(
            lat=lat,
            lon=lon,
            x_cm=_require_finite(data.get("x_cm"), "x_cm"),
            y_cm=_require_finite(data.get("y_cm"), "y_cm"),
            label=label,
        )


Affine = tuple[float, float, float, float, float, float]


@dataclass(frozen=True)
class PlanGeoreference:
    """Plan-level anchor mapping WGS84 (lat, lon) to CAD scene (x_cm, y_cm).

    ``origin_lat/lon`` is the GPS position of the scene point
    ``(origin_x_cm, origin_y_cm)``. ``cm_per_meter_x/y`` are the plan's centimetres
    per ground metre (100 for a true-scale plan). ``affine_matrix``, when present, is
    a 6-tuple ``(a, b, tx, c, d, ty)`` applied in scene cm on top of the base
    projection: ``x = a*x0 + b*y0 + tx``, ``y = c*x0 + d*y0 + ty``.
    """

    origin_lat: float
    origin_lon: float
    origin_x_cm: float = 0.0
    origin_y_cm: float = 0.0
    cm_per_meter_x: float = 100.0
    cm_per_meter_y: float = 100.0
    rotation_deg: float = 0.0
    control_points: tuple[ControlPoint, ...] = ()
    affine_matrix: Affine | None = None
    fit_north_angle_deg: float | None = None

    def effective_north(self, plan_north_deg: float) -> float:
        """The north angle the projection uses: the fit-time lock, else the plan's."""
        if self.affine_matrix is not None and self.fit_north_angle_deg is not None:
            return self.fit_north_angle_deg
        return plan_north_deg

    def __post_init__(self) -> None:
        lat, _lon = _require_lat_lon(self.origin_lat, self.origin_lon)
        if abs(lat) > MAX_ORIGIN_LAT_DEG:
            raise GeoreferenceError(
                f"origin_lat must be within ±{MAX_ORIGIN_LAT_DEG} degrees"
            )
        _require_finite(self.origin_x_cm, "origin_x_cm")
        _require_finite(self.origin_y_cm, "origin_y_cm")
        sx = _require_finite(self.cm_per_meter_x, "cm_per_meter_x")
        sy = _require_finite(self.cm_per_meter_y, "cm_per_meter_y")
        if sx <= 0.0 or sy <= 0.0:
            raise GeoreferenceError("cm_per_meter_x and cm_per_meter_y must be positive")
        _require_finite(self.rotation_deg, "rotation_deg")
        if len(self.control_points) > MAX_CONTROL_POINTS:
            raise GeoreferenceError(f"at most {MAX_CONTROL_POINTS} control points")
        for point in self.control_points:
            if not isinstance(point, ControlPoint):
                raise GeoreferenceError("control_points must be ControlPoint values")
        if self.affine_matrix is not None:
            if len(self.affine_matrix) != 6:
                raise GeoreferenceError("affine_matrix must have 6 finite values")
            vals = [
                _require_finite(v, f"affine_matrix[{i}]")
                for i, v in enumerate(self.affine_matrix)
            ]
            if abs(vals[0] * vals[4] - vals[1] * vals[3]) < _MIN_AFFINE_DET:
                raise GeoreferenceError("affine_matrix is singular")
        if self.fit_north_angle_deg is not None:
            _require_finite(self.fit_north_angle_deg, "fit_north_angle_deg")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "origin_lat": self.origin_lat,
            "origin_lon": self.origin_lon,
            "origin_x_cm": self.origin_x_cm,
            "origin_y_cm": self.origin_y_cm,
            "cm_per_meter_x": self.cm_per_meter_x,
            "cm_per_meter_y": self.cm_per_meter_y,
            "rotation_deg": self.rotation_deg,
        }
        if self.control_points:
            result["control_points"] = [cp.to_dict() for cp in self.control_points]
        if self.affine_matrix is not None:
            result["affine_matrix"] = list(self.affine_matrix)
        if self.fit_north_angle_deg is not None:
            result["fit_north_angle_deg"] = self.fit_north_angle_deg
        return result

    @classmethod
    def from_dict(cls, data: Any) -> PlanGeoreference:
        """Parse and validate a stored georeference; raises :class:`GeoreferenceError`."""
        if not isinstance(data, Mapping):
            raise GeoreferenceError("georeference must be an object")
        raw_cps = data.get("control_points") or []
        if not isinstance(raw_cps, (list, tuple)):
            raise GeoreferenceError("control_points must be a list")
        if len(raw_cps) > MAX_CONTROL_POINTS:
            raise GeoreferenceError(f"at most {MAX_CONTROL_POINTS} control points")
        raw_aff = data.get("affine_matrix")
        affine: Affine | None = None
        if raw_aff is not None:
            if not isinstance(raw_aff, (list, tuple)) or len(raw_aff) != 6:
                raise GeoreferenceError("affine_matrix must be a 6-element list")
            v = [_require_finite(x, f"affine_matrix[{i}]") for i, x in enumerate(raw_aff)]
            affine = (v[0], v[1], v[2], v[3], v[4], v[5])
        raw_fit = data.get("fit_north_angle_deg")
        fit_north = None if raw_fit is None else _require_finite(raw_fit, "fit_north_angle_deg")
        return cls(
            origin_lat=_require_finite(data.get("origin_lat"), "origin_lat"),
            origin_lon=_require_finite(data.get("origin_lon"), "origin_lon"),
            origin_x_cm=_require_finite(data.get("origin_x_cm", 0.0), "origin_x_cm"),
            origin_y_cm=_require_finite(data.get("origin_y_cm", 0.0), "origin_y_cm"),
            cm_per_meter_x=_require_finite(data.get("cm_per_meter_x", 100.0), "cm_per_meter_x"),
            cm_per_meter_y=_require_finite(data.get("cm_per_meter_y", 100.0), "cm_per_meter_y"),
            rotation_deg=_require_finite(data.get("rotation_deg", 0.0), "rotation_deg"),
            control_points=tuple(ControlPoint.from_dict(cp) for cp in raw_cps),
            affine_matrix=affine,
            fit_north_angle_deg=fit_north,
        )


def georeference_from_plan(value: object) -> PlanGeoreference | None:
    """The georeference a loaded plan carries, or ``None`` for a missing/bad one.

    Load is a trust boundary (like ``plan_north.north_angle_from_plan``): an
    unreadable value is dropped with a log line rather than keeping the plan from
    opening. A non-finite number never reaches here - ``plan_io`` refuses it.
    """
    if value is None:
        return None
    try:
        return PlanGeoreference.from_dict(value)
    except GeoreferenceError:
        import logging

        logging.getLogger(__name__).warning("plan georeference: ignoring an invalid value")
        return None


def georeference_from_legacy_block(raw: object, north_angle_deg: float = 0.0) -> dict[str, Any] | None:
    """The ``metadata.georeference`` dict for a plan saved with the older top-level ``georef`` key.

    That key came from a parallel georeference that never shipped (``origin_lat``/``origin_lon``
    at scene (0, 0), ``scale_meters_per_unit``, optional ``reference_points`` with ``x``/``y`` in
    scene units; its rotation was the plan's north, stored separately). Read once on load so a
    plan georeferenced by that build keeps its place; the next save writes the record's key
    only. Surveyed reference points are fitted back in (as a calibration locked to the plan's
    north) so they land where they were measured. ``None`` for anything unusable.
    """
    if not isinstance(raw, Mapping):
        return None
    try:
        scale = _require_finite(raw.get("scale_meters_per_unit", 0.01), "scale_meters_per_unit")
        if scale <= 0.0:
            return None
        per_meter = 1.0 / scale
        base = PlanGeoreference(
            origin_lat=_require_finite(raw.get("origin_lat"), "origin_lat"),
            origin_lon=_require_finite(raw.get("origin_lon"), "origin_lon"),
            cm_per_meter_x=per_meter,
            cm_per_meter_y=per_meter,
        )
        points: list[ControlPoint] = []
        raw_points = raw.get("reference_points")
        if isinstance(raw_points, (list, tuple)):
            for item in raw_points[:MAX_CONTROL_POINTS]:
                try:
                    points.append(
                        ControlPoint(
                            lat=_require_finite(item.get("lat"), "lat"),
                            lon=_require_finite(item.get("lon"), "lon"),
                            x_cm=_require_finite(item.get("x"), "x"),
                            y_cm=_require_finite(item.get("y"), "y"),
                            label=item.get("name") if isinstance(item.get("name"), str) else "",
                        )
                    )
                except (GeoreferenceError, AttributeError):
                    continue
        if points:
            try:
                return fit_affine_calibration(base, points, north_angle_deg).georeference.to_dict()
            except GeoreferenceError:
                pass  # degenerate points: keep the anchor alone
        return base.to_dict()
    except GeoreferenceError:
        return None


def _base_gps_to_scene(
    lat: float, lon: float, georef: PlanGeoreference, north_angle_deg: float
) -> tuple[float, float]:
    """Project WGS84 to scene cm WITHOUT the affine residual correction."""
    m_per_lat, m_per_lon = meters_per_degree(georef.origin_lat)
    north_m = (lat - georef.origin_lat) * m_per_lat
    east_m = (lon - georef.origin_lon) * m_per_lon
    theta = math.radians(north_angle_deg + georef.rotation_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    # east = (cos t, -sin t), north = (sin t, cos t) in the plan frame.
    x_rel_m = east_m * cos_t + north_m * sin_t
    y_rel_m = -east_m * sin_t + north_m * cos_t
    return (
        georef.origin_x_cm + x_rel_m * georef.cm_per_meter_x,
        georef.origin_y_cm + y_rel_m * georef.cm_per_meter_y,
    )


def gps_to_scene(
    lat: float, lon: float, georef: PlanGeoreference, north_angle_deg: float = 0.0
) -> tuple[float, float]:
    """Convert WGS84 ``(lat, lon)`` to CAD scene ``(x_cm, y_cm)``."""
    lat_f, lon_f = _require_lat_lon(lat, lon)
    north_f = georef.effective_north(_require_finite(north_angle_deg, "north_angle_deg"))
    x0, y0 = _base_gps_to_scene(lat_f, lon_f, georef, north_f)
    if georef.affine_matrix is None:
        return _finite_pair(x0, y0, "scene position")
    a, b, tx, c, d, ty = georef.affine_matrix
    return _finite_pair(a * x0 + b * y0 + tx, c * x0 + d * y0 + ty, "scene position")


def scene_to_gps(
    x_cm: float, y_cm: float, georef: PlanGeoreference, north_angle_deg: float = 0.0
) -> tuple[float, float]:
    """Convert CAD scene ``(x_cm, y_cm)`` to WGS84 ``(lat, lon)`` - the exact inverse."""
    x_f = _require_finite(x_cm, "x_cm")
    y_f = _require_finite(y_cm, "y_cm")
    north_f = georef.effective_north(_require_finite(north_angle_deg, "north_angle_deg"))

    if georef.affine_matrix is not None:
        a, b, tx, c, d, ty = georef.affine_matrix
        det = a * d - b * c
        dx, dy = x_f - tx, y_f - ty
        x0 = (d * dx - b * dy) / det
        y0 = (-c * dx + a * dy) / det
    else:
        x0, y0 = x_f, y_f

    x_rel_m = (x0 - georef.origin_x_cm) / georef.cm_per_meter_x
    y_rel_m = (y0 - georef.origin_y_cm) / georef.cm_per_meter_y
    theta = math.radians(north_f + georef.rotation_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    east_m = x_rel_m * cos_t - y_rel_m * sin_t
    north_m = x_rel_m * sin_t + y_rel_m * cos_t

    m_per_lat, m_per_lon = meters_per_degree(georef.origin_lat)
    return _finite_pair(
        georef.origin_lat + north_m / m_per_lat,
        georef.origin_lon + east_m / m_per_lon,
        "GPS position",
    )


def _solve_3x3(mat: list[list[float]], rhs: list[float]) -> tuple[float, float, float]:
    """Solve a 3x3 linear system with partial pivoting (stdlib-only)."""
    aug = [mat[i][:] + [rhs[i]] for i in range(3)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-9:
            raise GeoreferenceError("control points are collinear or degenerate")
        aug[col], aug[pivot] = aug[pivot], aug[col]
        div = aug[col][col]
        for j in range(col, 4):
            aug[col][j] /= div
        for r in range(3):
            if r != col:
                factor = aug[r][col]
                for j in range(col, 4):
                    aug[r][j] -= factor * aug[col][j]
    return aug[0][3], aug[1][3], aug[2][3]


@dataclass(frozen=True)
class CalibrationResult:
    """Result of fitting ground-control points onto a :class:`PlanGeoreference`."""

    georeference: PlanGeoreference
    residuals_cm: tuple[float, ...]
    rms_error_cm: float
    method: str  # "translation" | "similarity" | "affine"


def fit_affine_calibration(
    base: PlanGeoreference,
    control_points: Iterable[ControlPoint],
    north_angle_deg: float = 0.0,
) -> CalibrationResult:
    """Fit a residual correction from ``control_points`` onto ``base``.

    - 1 point: pure translation lock.
    - 2 points: 2-D similarity (translation + rotation + uniform scale).
    - >= 3 points: least-squares 2-D affine transform.

    The base projection's own affine (if any) is replaced, never composed, so
    re-calibrating is idempotent. Residuals are reported in scene cm.
    """
    pts = tuple(control_points)
    if not pts:
        raise GeoreferenceError("at least one control point is required")
    if len(pts) > MAX_CONTROL_POINTS:
        raise GeoreferenceError(f"at most {MAX_CONTROL_POINTS} control points")
    north_f = _require_finite(north_angle_deg, "north_angle_deg")

    src = [_base_gps_to_scene(cp.lat, cp.lon, base, north_f) for cp in pts]
    dst = [(cp.x_cm, cp.y_cm) for cp in pts]

    matrix: Affine
    if len(pts) == 1:
        method = "translation"
        matrix = (1.0, 0.0, dst[0][0] - src[0][0], 0.0, 1.0, dst[0][1] - src[0][1])
    elif len(pts) == 2:
        method = "similarity"
        dx0, dy0 = src[1][0] - src[0][0], src[1][1] - src[0][1]
        denom = dx0 * dx0 + dy0 * dy0
        if denom < 1e-9:
            raise GeoreferenceError("two control points cannot share the same GPS location")
        dx1, dy1 = dst[1][0] - dst[0][0], dst[1][1] - dst[0][1]
        if dx1 * dx1 + dy1 * dy1 < 1e-9:
            raise GeoreferenceError("two control points cannot share the same scene location")
        a = (dx0 * dx1 + dy0 * dy1) / denom
        b = (dx0 * dy1 - dy0 * dx1) / denom
        tx = dst[0][0] - (a * src[0][0] - b * src[0][1])
        ty = dst[0][1] - (b * src[0][0] + a * src[0][1])
        matrix = (a, -b, tx, b, a, ty)
    else:
        method = "affine"
        ata = [[0.0] * 3 for _ in range(3)]
        atx = [0.0, 0.0, 0.0]
        aty = [0.0, 0.0, 0.0]
        for (x0, y0), (x1, y1) in zip(src, dst, strict=True):
            row = (x0, y0, 1.0)
            for i in range(3):
                atx[i] += row[i] * x1
                aty[i] += row[i] * y1
                for j in range(3):
                    ata[i][j] += row[i] * row[j]
        a, b, tx = _solve_3x3(ata, atx)
        c, d, ty = _solve_3x3(ata, aty)
        matrix = (a, b, tx, c, d, ty)

    try:
        calibrated = PlanGeoreference(
            origin_lat=base.origin_lat,
            origin_lon=base.origin_lon,
            origin_x_cm=base.origin_x_cm,
            origin_y_cm=base.origin_y_cm,
            cm_per_meter_x=base.cm_per_meter_x,
            cm_per_meter_y=base.cm_per_meter_y,
            rotation_deg=base.rotation_deg,
            control_points=pts,
            affine_matrix=matrix,
            fit_north_angle_deg=north_f,
        )
    except GeoreferenceError as error:
        raise GeoreferenceError(f"control points give a degenerate calibration: {error}") from error

    residuals = tuple(
        math.hypot(px - cp.x_cm, py - cp.y_cm)
        for cp in pts
        for px, py in [gps_to_scene(cp.lat, cp.lon, calibrated, north_f)]
    )
    rms = math.sqrt(sum(r * r for r in residuals) / len(residuals))
    return CalibrationResult(calibrated, residuals, rms, method)


def georeference_bounds(
    georef: PlanGeoreference,
    canvas_width_cm: float,
    canvas_height_cm: float,
    north_angle_deg: float = 0.0,
) -> dict[str, Any]:
    """The canvas rectangle's four corners (and centre) as ``(lat, lon)``.

    Corners are named by their scene position (south_west is scene ``(0, 0)``); with
    a turned north they are not the compass corners, so the compass extent
    (``min/max`` lat and lon) is reported as well.
    """
    w = _require_finite(canvas_width_cm, "canvas_width_cm")
    h = _require_finite(canvas_height_cm, "canvas_height_cm")
    spots = {
        "south_west": (0.0, 0.0),
        "south_east": (w, 0.0),
        "north_east": (w, h),
        "north_west": (0.0, h),
        "center": (w / 2.0, h / 2.0),
    }
    corners = {
        name: scene_to_gps(x, y, georef, north_angle_deg) for name, (x, y) in spots.items()
    }
    lats = [lat for lat, _ in corners.values()]
    lons = [lon for _, lon in corners.values()]
    return {
        "corners": {name: {"lat": lat, "lon": lon} for name, (lat, lon) in corners.items()},
        "min_lat": min(lats),
        "max_lat": max(lats),
        "min_lon": min(lons),
        "max_lon": max(lons),
    }


def georeference_from_satellite(
    geo_metadata: Mapping[str, Any],
    anchor_x_cm: float,
    anchor_y_cm: float,
    north_angle_deg: float = 0.0,
) -> PlanGeoreference:
    """The georeference of a plan whose satellite background is centred on a scene point.

    ``geo_metadata`` is what a satellite import stores on its ``BackgroundImageItem``
    (``center`` = ``[lat, lon]``). The picture is north-up, so the plan point
    ``(anchor_x_cm, anchor_y_cm)`` is the picture's centre and the scale is the true
    100 cm per metre; ``rotation_deg`` cancels the plan's own north angle so the bound
    mapping reproduces the picture as it was placed.
    """
    center = geo_metadata.get("center") if isinstance(geo_metadata, Mapping) else None
    if not isinstance(center, (list, tuple)) or len(center) != 2:
        raise GeoreferenceError("satellite metadata has no centre")
    lat, lon = _require_lat_lon(center[0], center[1])
    north_f = _require_finite(north_angle_deg, "north_angle_deg")
    return PlanGeoreference(
        origin_lat=lat,
        origin_lon=lon,
        origin_x_cm=_require_finite(anchor_x_cm, "anchor_x_cm"),
        origin_y_cm=_require_finite(anchor_y_cm, "anchor_y_cm"),
        rotation_deg=-north_f,
    )


__all__ = [
    "MAX_CONTROL_POINTS",
    "MAX_ORIGIN_LAT_DEG",
    "CalibrationResult",
    "ControlPoint",
    "GeoreferenceError",
    "PlanGeoreference",
    "fit_affine_calibration",
    "georeference_bounds",
    "georeference_from_legacy_block",
    "georeference_from_plan",
    "georeference_from_satellite",
    "gps_to_scene",
    "meters_per_degree",
    "scene_to_gps",
]
