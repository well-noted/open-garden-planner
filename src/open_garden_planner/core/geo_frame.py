"""A Qt-free GPS frame over the plan's georeference, for field work (ADR-079, ADR-080).

``core/georeference.py`` is the one georeference of record: a ``PlanGeoreference`` plus
the plan's north angle give every scene <-> GPS conversion (affine calibration and the
fit-time north lock included). Field Remote, the photo tools and the positioning engine
also think in *east/north metres from the plan's origin* (ENU) and in *the compass angle
of the plan*. :class:`GeoFrame` is that view, built on top of the record and never
stored: it holds a ``PlanGeoreference`` and the plan's north angle, and answers

- ``gps_to_canvas`` / ``canvas_to_gps``: exactly ``gps_to_scene`` / ``scene_to_gps``;
- ``gps_to_enu`` / ``enu_to_gps``: metres east/north of ``(origin_lat, origin_lon)`` on
  the same WGS84 tangent plane the record uses (``meters_per_degree``);
- ``rotation_deg``: where TRUE NORTH points on the canvas, degrees clockwise from
  plan-up, *measured through the real mapping* so a calibrated (affine) georeference
  reports the turn it actually applies;
- ``scale_meters_per_unit`` and ``fit_rms_m``, derived the same way.

No second georeference model exists: a frame is rebuilt from the plan each time it is
needed (``frame_from_project`` on the main thread, ``frame_from_snapshot`` anywhere).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from open_garden_planner.core.georeference import (
    GeoreferenceError,
    PlanGeoreference,
    georeference_from_plan,
    gps_to_scene,
    meters_per_degree,
    scene_to_gps,
)
from open_garden_planner.core.georeference import (
    _require_finite as _finite,
)
from open_garden_planner.core.georeference import (
    _require_lat_lon as validate_lat_lon,
)
from open_garden_planner.core.plan_north import north_angle_from_plan

#: Metres per scene unit for a centimetre canvas.
DEFAULT_SCALE_M_PER_UNIT = 0.01

__all__ = [
    "DEFAULT_SCALE_M_PER_UNIT",
    "GeoFrame",
    "GeoreferenceError",
    "frame_from_project",
    "frame_from_snapshot",
    "make_frame",
    "validate_lat_lon",
]


@dataclass(frozen=True)
class GeoFrame:
    """The plan's georeference plus its north angle: GPS, ENU metres and scene cm."""

    georeference: PlanGeoreference
    north_angle_deg: float = 0.0

    # -- the anchor ---------------------------------------------------------

    @property
    def origin_lat(self) -> float:
        return self.georeference.origin_lat

    @property
    def origin_lon(self) -> float:
        return self.georeference.origin_lon

    # -- GPS <-> scene (the record's own projection) ------------------------

    def gps_to_canvas(self, lat: float, lon: float) -> tuple[float, float]:
        """Scene ``(x, y)`` in cm of a GPS position (calibration and north lock included)."""
        return gps_to_scene(lat, lon, self.georeference, self.north_angle_deg)

    def canvas_to_gps(self, x: float, y: float) -> tuple[float, float]:
        """GPS ``(lat, lon)`` of a scene point."""
        return scene_to_gps(x, y, self.georeference, self.north_angle_deg)

    # -- GPS <-> ENU metres from the origin ---------------------------------

    def gps_to_enu(self, lat: float, lon: float) -> tuple[float, float]:
        """East/north metres of ``(lat, lon)`` from ``(origin_lat, origin_lon)``."""
        la, lo = validate_lat_lon(lat, lon)
        m_lat, m_lon = meters_per_degree(self.origin_lat)
        return (lo - self.origin_lon) * m_lon, (la - self.origin_lat) * m_lat

    def enu_to_gps(self, east: float, north: float) -> tuple[float, float]:
        """Inverse of :meth:`gps_to_enu`."""
        m_lat, m_lon = meters_per_degree(self.origin_lat)
        lat = self.origin_lat + _finite(north, "north") / m_lat
        lon = self.origin_lon + _finite(east, "east") / m_lon
        if abs(lat) > 90.0 or abs(lon) > 180.0:
            raise GeoreferenceError("the point is too far from the origin for this georeference")
        return lat, lon

    # -- derived plan facts -------------------------------------------------

    def _enu_to_canvas(self, east: float, north: float) -> tuple[float, float]:
        return self.gps_to_canvas(*self.enu_to_gps(east, north))

    @property
    def rotation_deg(self) -> float:
        """True north on the canvas, degrees clockwise from plan-up, in ``[0, 360)``."""
        x0, y0 = self._enu_to_canvas(0.0, 0.0)
        x1, y1 = self._enu_to_canvas(0.0, 1.0)
        return math.degrees(math.atan2(x1 - x0, y1 - y0)) % 360.0

    @property
    def scale_meters_per_unit(self) -> float:
        """Ground metres per scene unit (0.01 for a true-scale centimetre canvas)."""
        x0, y0 = self._enu_to_canvas(0.0, 0.0)
        ex, ey = self._enu_to_canvas(1.0, 0.0)
        nx, ny = self._enu_to_canvas(0.0, 1.0)
        det = abs((ex - x0) * (ny - y0) - (ey - y0) * (nx - x0))
        return 1.0 / math.sqrt(det) if det > 0.0 else DEFAULT_SCALE_M_PER_UNIT

    @property
    def fit_rms_m(self) -> float | None:
        """RMS of the calibration's control-point residuals in metres, or ``None``."""
        geo = self.georeference
        if geo.affine_matrix is None or not geo.control_points:
            return None
        total = 0.0
        for cp in geo.control_points:
            px, py = self.gps_to_canvas(cp.lat, cp.lon)
            total += (px - cp.x_cm) ** 2 + (py - cp.y_cm) ** 2
        return math.sqrt(total / len(geo.control_points)) * self.scale_meters_per_unit

    @property
    def source(self) -> str:
        """``fit`` for a calibrated georeference, else ``manual``."""
        return "fit" if self.georeference.affine_matrix is not None else "manual"

    def to_dict(self) -> dict[str, Any]:
        """The record's own dict plus the plan north (a stable cache key for sessions)."""
        data = self.georeference.to_dict()
        data["north_angle_deg"] = self.north_angle_deg
        return data


def make_frame(
    origin_lat: float,
    origin_lon: float,
    *,
    scale_meters_per_unit: float = DEFAULT_SCALE_M_PER_UNIT,
    rotation_deg: float = 0.0,
) -> GeoFrame:
    """An uncalibrated frame: scene origin at ``(origin_lat, origin_lon)``, plan north *rotation_deg*."""
    scale = _finite(scale_meters_per_unit, "scale_meters_per_unit")
    if scale <= 0.0:
        raise GeoreferenceError("scale_meters_per_unit must be positive")
    per_meter = 1.0 / scale
    return GeoFrame(
        PlanGeoreference(
            origin_lat=origin_lat,
            origin_lon=origin_lon,
            cm_per_meter_x=per_meter,
            cm_per_meter_y=per_meter,
        ),
        _finite(rotation_deg, "rotation_deg") % 360.0,
    )


def frame_from_project(project_manager: Any) -> GeoFrame | None:
    """The open plan's frame, or ``None`` when it is not georeferenced (main thread)."""
    geo = project_manager.georeference
    if geo is None:
        return None
    return GeoFrame(geo, float(project_manager.north_angle_deg))


def frame_from_snapshot(snapshot: Mapping[str, Any]) -> GeoFrame | None:
    """The frame of an agent snapshot (``.ogp``-shaped), or ``None`` for an unusable block.

    Load is a trust boundary: an unreadable georeference reads as "not georeferenced".
    """
    meta = snapshot.get("metadata")
    raw = meta.get("georeference") if isinstance(meta, Mapping) else None
    geo = georeference_from_plan(raw)
    if geo is None:
        return None
    return GeoFrame(geo, north_angle_from_plan(snapshot.get("north_angle_deg")))
