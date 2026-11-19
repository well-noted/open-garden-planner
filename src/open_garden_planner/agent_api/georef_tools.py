"""Qt-free helpers behind the Agent API georeference tools (ADR-079).

Pure functions over plain data so the server, the app host and the tests share one
implementation of: the read model of a plan's georeference, the reference-point
list an agent passes (explicit canvas coordinates, or an existing plan object's
centre or vertex), and the ground box a ``set_initial_map`` call asks Google for.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from open_garden_planner.core.georef import (
    GeoReference,
    GeorefError,
    ReferencePoint,
    georef_from_plan,
    validate_lat_lon,
)
from open_garden_planner.core.plan_north import north_angle_from_plan

#: ``set_initial_map`` ground-box limits, metres. 9 tiles at best resolution.
MIN_AREA_M = 10.0
MAX_AREA_M = 1000.0
DEFAULT_AREA_M = 100.0
MAX_POINTS = 200

#: Resolves ``(item_id, vertex_index | None)`` to the object's scene ``(x, y)`` in cm.
PointResolver = Callable[[str, "int | None"], "tuple[float, float]"]


def georef_from_snapshot(snapshot: Mapping[str, Any]) -> GeoReference | None:
    """The georeference of a ``snapshot_dict`` (rotation = the plan's north)."""
    north = north_angle_from_plan(snapshot.get("north_angle_deg"))
    return georef_from_plan(snapshot.get("georef"), north)


def georef_info(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """The ``get_georeference`` payload for a plan snapshot."""
    return info_from_georef(
        georef_from_snapshot(snapshot), north_angle_from_plan(snapshot.get("north_angle_deg"))
    )


def info_from_georef(geo: GeoReference | None, north: float) -> dict[str, Any]:
    """The ``GeoreferenceInfo`` payload for a georeference (or ``None``) and the plan north."""
    if geo is None:
        return {"georeferenced": False, "north_angle_deg": north}
    return {
        "georeferenced": True,
        "origin_lat": geo.origin_lat,
        "origin_lon": geo.origin_lon,
        "scale_meters_per_unit": geo.scale_meters_per_unit,
        "rotation_deg": geo.rotation_deg,
        "north_angle_deg": north,
        "parcel_boundary": [[la, lo] for la, lo in geo.parcel_boundary] if geo.parcel_boundary else None,
        "reference_points": [p.to_dict() for p in geo.reference_points],
        "fit_rms_m": geo.fit_rms_m,
        "source": geo.source,
    }


def parse_reference_points(
    raw: object, resolve: PointResolver | None = None
) -> list[ReferencePoint]:
    """Turn an agent's point list into validated :class:`ReferencePoint` objects.

    Each entry is an object with ``lat`` and ``lon`` plus EITHER explicit
    canvas ``x``/``y`` (cm) OR an ``item_id`` of an existing plan object (its
    centre, or vertex ``vertex_index`` when given), which *resolve* turns into
    canvas coordinates. ``name`` is optional. Raises ``ValueError`` naming the
    offending entry.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("points must be a list of objects")
    if not raw:
        raise ValueError("at least one reference point is required")
    if len(raw) > MAX_POINTS:
        raise ValueError(f"at most {MAX_POINTS} reference points are accepted")
    out: list[ReferencePoint] = []
    for index, entry in enumerate(raw):
        label = f"point {index + 1}"
        if not isinstance(entry, Mapping):
            raise ValueError(f"{label} must be an object")
        has_xy = entry.get("x") is not None or entry.get("y") is not None
        item_id = entry.get("item_id")
        if has_xy == (item_id is not None):
            raise ValueError(f"{label}: give either x and y, or item_id (not both, not neither)")
        try:
            lat, lon = validate_lat_lon(entry.get("lat"), entry.get("lon"))
            if item_id is not None:
                if resolve is None or not isinstance(item_id, str):
                    raise ValueError("item_id must be an object id string")
                vertex = entry.get("vertex_index")
                if vertex is not None and (isinstance(vertex, bool) or not isinstance(vertex, int)):
                    raise ValueError("vertex_index must be an integer")
                coords: tuple[Any, Any] = resolve(item_id, vertex)
            else:
                coords = (entry.get("x"), entry.get("y"))
            name = entry.get("name")
            if name is None and isinstance(item_id, str):
                name = item_id
            out.append(
                ReferencePoint.from_dict(
                    {"name": name if isinstance(name, str) else "", "lat": lat, "lon": lon, "x": coords[0], "y": coords[1]}
                )
            )
        except (GeorefError, ValueError) as error:
            raise ValueError(f"{label}: {error}") from error
    return out


def area_box(
    lat: float, lon: float, width_m: float | None, height_m: float | None
) -> tuple[tuple[float, float], tuple[float, float]]:
    """``(nw, se)`` ``(lat, lon)`` corners of a north-up ground box centred on a point.

    *width_m* / *height_m* default to 100 m (height defaults to the width) and
    must lie in ``[10, 1000]`` metres.
    """
    la, lo = validate_lat_lon(lat, lon)
    w = DEFAULT_AREA_M if width_m is None else width_m
    h = w if height_m is None else height_m
    for name, value in (("width_m", w), ("height_m", h)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
        if not MIN_AREA_M <= value <= MAX_AREA_M:
            raise ValueError(f"{name} must be between {MIN_AREA_M:g} and {MAX_AREA_M:g} metres")
    if abs(la) > 85.0:
        raise ValueError("satellite maps are not available beyond latitude +-85 degrees")
    frame = GeoReference(la, lo)
    nw = frame.enu_to_gps(-w / 2.0, h / 2.0)
    se = frame.enu_to_gps(w / 2.0, -h / 2.0)
    return nw, se
