"""Read-only site-measurement and object-height queries over the Agent API snapshot.

Thin wrappers: the geometry lives in :mod:`open_garden_planner.core.site_measure`
and the height ladder in :mod:`open_garden_planner.core.object_height` (the one
the 3D view and the shadows read). Qt-free; nothing here mutates the plan, so no
undo entry and no dirty flag. Units: scene centimetres (ADR-078), Y-up.

Error shape: an unknown object id returns ``None`` (as ``measure_distance``);
malformed or untrusted input (NaN, infinity, absurd coordinates, too many
points, a wrong point form) raises ``ValueError``, which MCP reports as a tool
error with that message.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from open_garden_planner.agent_api import queries
from open_garden_planner.agent_api.schema import (
    AreaMeasurement,
    Clearance,
    LengthMeasurement,
    ObjectHeight,
    ObjectHeightRow,
    ObjectHeightSummary,
    PathMeasurement,
    PlanPoint,
    PointMeasurement,
    PointToObject,
    ResolvedPoint,
    SiteBoundary,
    SiteExtents,
)
from open_garden_planner.core import object_height as oh
from open_garden_planner.core import site_measure as sm
from open_garden_planner.core.growth_model import (
    current_height_from_metadata,
    effective_current_height_cm,
)

#: ``id_a``/``id_b`` of ``measure_clearance`` may be this literal instead of an id.
BOUNDARY_ALIAS = "boundary"
MAX_LIST_LIMIT = 200
_BOUNDARY_NAME = re.compile(r"\b(boundary|property[ _-]?line|parcel)\b", re.IGNORECASE)
_PLANT_TYPES = frozenset({"TREE", "SHRUB", "PERENNIAL"})


def _pt(p: sm.Pt) -> PlanPoint:
    return PlanPoint(x_cm=p[0], y_cm=p[1])


def _m(cm: float) -> float:
    return cm / 100.0


# --- boundary ---------------------------------------------------------------


def _is_boundary(obj: Mapping[str, Any]) -> bool:
    name = obj.get("name")
    return isinstance(name, str) and bool(_BOUNDARY_NAME.search(name)) and obj.get("type") in (
        "polyline", "polygon", "rectangle")


def _boundary_shape(snapshot: dict[str, Any]) -> tuple[dict[str, Any] | None, sm.Shape | None, str]:
    """``(object, shape, origin)`` of the plan's boundary: a named boundary
    wall/line object first, else the georeference's parcel ring."""
    for obj in snapshot.get("objects") or []:
        if _is_boundary(obj):
            return obj, sm.shape_from_object(obj), "object"
    raw = (snapshot.get("georef") or {}).get("parcel_boundary") if isinstance(
        snapshot.get("georef"), Mapping) else None
    link = sm.GeoLink.from_snapshot(snapshot)
    if raw and link is not None:
        ring = tuple(link.to_plan(float(p[0]), float(p[1])) for p in raw)
        if len(ring) >= 3:
            return None, sm._make_shape("polygon", [sm.Part("ring", ring)], len(ring)), "georef"
    return None, None, "object"


def _resolve_shape(snapshot: dict[str, Any], item_id: str) -> tuple[dict[str, Any] | None, sm.Shape | None]:
    if item_id == BOUNDARY_ALIAS:
        obj, shape, _ = _boundary_shape(snapshot)
        if shape is not None:
            return obj or {"item_id": BOUNDARY_ALIAS, "name": "parcel boundary"}, shape
    obj = queries._find(snapshot, item_id)
    if obj is None:
        return None, None
    return obj, sm.shape_from_object(obj)


def _label(obj: Mapping[str, Any]) -> tuple[str | None, str | None]:
    return obj.get("name"), obj.get("object_type")


# --- area / length / clearance ----------------------------------------------


def measure_area(snapshot: dict[str, Any], item_id: str) -> AreaMeasurement | None:
    obj = queries._find(snapshot, item_id)
    if obj is None:
        return None
    shape = sm.shape_from_object(obj)
    if shape is None:
        raise ValueError("a background image has no measurable area")
    area, perimeter, holes = sm.area_and_perimeter(shape)
    hole_perimeter = sum(
        sm.polyline_length(h, True) for part in shape.parts for h in part.holes)
    name, otype = _label(obj)
    return AreaMeasurement(
        item_id=str(item_id), name=name, object_type=otype, geometry=shape.geometry,
        area_cm2=area, area_m2=area / 1e4, perimeter_cm=perimeter, perimeter_m=_m(perimeter),
        hole_count=holes, hole_perimeter_cm=hole_perimeter)


def measure_length(snapshot: dict[str, Any], item_id: str) -> LengthMeasurement | None:
    obj = queries._find(snapshot, item_id)
    if obj is None:
        return None
    shape = sm.shape_from_object(obj)
    if shape is None:
        raise ValueError("a background image has no measurable length")
    length, closed = sm.length_of(shape)
    name, otype = _label(obj)
    return LengthMeasurement(
        item_id=str(item_id), name=name, object_type=otype, geometry=shape.geometry,
        length_cm=length, length_m=_m(length), closed=closed, vertex_count=shape.vertex_count,
        curved=any(p.curved for p in shape.parts))


def measure_clearance(snapshot: dict[str, Any], id_a: str, id_b: str) -> Clearance | None:
    obj_a, shape_a = _resolve_shape(snapshot, id_a)
    obj_b, shape_b = _resolve_shape(snapshot, id_b)
    if obj_a is None or obj_b is None:
        return None
    if shape_a is None or shape_b is None:
        raise ValueError("a background image has no edges to measure")
    result = sm.clearance(shape_a, shape_b)
    return Clearance(
        id_a=str(id_a), id_b=str(id_b), distance_cm=result.distance_cm,
        distance_m=_m(result.distance_cm), overlaps=result.overlaps, relation=result.relation,
        point_a=_pt(result.point_a), point_b=_pt(result.point_b))


# --- points -----------------------------------------------------------------


def resolve_point(snapshot: dict[str, Any], spec: object) -> ResolvedPoint:
    """A point form -> scene cm: ``{x_cm, y_cm}``, ``{lat, lon}`` or an object id string."""
    if isinstance(spec, str):
        obj = queries._find(snapshot, spec)
        if obj is None:
            raise ValueError(f"unknown object id {spec!r}")
        cx, cy = queries.object_center(obj)
        return ResolvedPoint(x_cm=cx, y_cm=cy, kind="object", object_id=spec)
    if not isinstance(spec, Mapping):
        raise ValueError("a point is {x_cm, y_cm}, {lat, lon} or an object id string")
    if "x_cm" in spec or "y_cm" in spec:
        if "x_cm" not in spec or "y_cm" not in spec:
            raise ValueError("a plan point needs both x_cm and y_cm")
        return ResolvedPoint(x_cm=sm.finite_cm(spec["x_cm"], "x_cm"),
                             y_cm=sm.finite_cm(spec["y_cm"], "y_cm"), kind="plan")
    if "lat" in spec or "lon" in spec:
        if "lat" not in spec or "lon" not in spec:
            raise ValueError("a geographic point needs both lat and lon")
        link = sm.GeoLink.from_snapshot(snapshot)
        if link is None:
            raise ValueError(
                "this plan has no georeference, so a {lat, lon} point cannot be placed on it; "
                "use plan points {x_cm, y_cm} or object ids")
        lat = sm.finite_cm(spec["lat"], "lat")
        lon = sm.finite_cm(spec["lon"], "lon")
        x, y = link.to_plan(lat, lon)
        sm.finite_cm(x, "x_cm")
        sm.finite_cm(y, "y_cm")
        return ResolvedPoint(x_cm=x, y_cm=y, kind="geo")
    raise ValueError("a point is {x_cm, y_cm}, {lat, lon} or an object id string")


def _north(snapshot: dict[str, Any]) -> float:
    from open_garden_planner.core.plan_north import north_angle_from_plan

    return north_angle_from_plan(snapshot.get("north_angle_deg"))


def measure_points(snapshot: dict[str, Any], a: object, b: object) -> PointMeasurement:
    pa, pb = resolve_point(snapshot, a), resolve_point(snapshot, b)
    start, end = (pa.x_cm, pa.y_cm), (pb.x_cm, pb.y_cm)
    north = _north(snapshot)
    dist = sm.distance(start, end)
    return PointMeasurement(
        a=pa, b=pb, distance_cm=dist, distance_m=_m(dist),
        dx_cm=end[0] - start[0], dy_cm=end[1] - start[1],
        plan_bearing_deg=sm.plan_bearing_deg(start, end),
        compass_bearing_deg=sm.compass_bearing_deg(start, end, north),
        north_angle_deg=north)


def measure_path(snapshot: dict[str, Any], points: Sequence[object], closed: bool = False) -> PathMeasurement:
    if isinstance(points, (str, bytes)) or not isinstance(points, Sequence):
        raise ValueError("points must be a list of 2 to 200 points")
    if not 2 <= len(points) <= sm.MAX_PATH_POINTS:
        raise ValueError(
            f"a path needs 2 to {sm.MAX_PATH_POINTS} points, got {len(points)}")
    resolved = [resolve_point(snapshot, p) for p in points]
    coords = [(p.x_cm, p.y_cm) for p in resolved]
    legs = sm.leg_lengths(coords, bool(closed))
    total = sum(legs)
    return PathMeasurement(points=resolved, leg_lengths_cm=legs, total_cm=total,
                           total_m=_m(total), closed=bool(closed) and len(coords) > 2)


def measure_to_object(snapshot: dict[str, Any], point: object, item_id: str) -> PointToObject | None:
    obj, shape = _resolve_shape(snapshot, item_id)
    if obj is None:
        return None
    if shape is None:
        raise ValueError("a background image has no edges to measure")
    resolved = resolve_point(snapshot, point)
    res = sm.point_to_shape((resolved.x_cm, resolved.y_cm), shape)
    return PointToObject(
        item_id=str(item_id), name=obj.get("name"), point=resolved,
        distance_cm=res.distance_cm, distance_m=_m(res.distance_cm), inside=res.inside,
        edge_distance_cm=res.edge_distance_cm, nearest_point=_pt(res.nearest))


# --- heights ----------------------------------------------------------------


def _plan_date(snapshot: dict[str, Any], obj: Mapping[str, Any] | None = None) -> tuple[date | None, str | None]:
    """The plan date the snapshot was evaluated at (never the wall clock)."""
    raw = (obj or {}).get("as_of_date") or (snapshot.get("agent_meta") or {}).get("as_of_date")
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw), raw
        except ValueError:
            return None, None
    return None, None


def _height_of(snapshot: dict[str, Any], obj: Mapping[str, Any]) -> ObjectHeight:
    otype = obj.get("object_type")
    metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else None
    at, iso = _plan_date(snapshot, obj)
    height, source = oh.resolve_height(otype or "", metadata, at)
    current = mature = None
    if otype in _PLANT_TYPES:
        species = (metadata or {}).get("plant_species")
        if isinstance(species, dict):
            current = effective_current_height_cm(species, metadata)
            raw_max = species.get("max_height_cm")
            if isinstance(raw_max, (int, float)) and not isinstance(raw_max, bool) and raw_max > 0:
                mature = float(raw_max)
        else:
            current = current_height_from_metadata(metadata)
        if source in (oh.SOURCE_GROWN, oh.SOURCE_CURRENT):
            current = height
    return ObjectHeight(
        item_id=str(obj.get("item_id", "")), name=obj.get("name"), object_type=otype,
        height_cm=height, height_m=None if height is None else _m(height), source=source,
        as_of_date=iso, current_height_cm=current, mature_height_cm=mature)


def get_object_height(snapshot: dict[str, Any], item_id: str) -> ObjectHeight | None:
    obj = queries._find(snapshot, item_id)
    return None if obj is None else _height_of(snapshot, obj)


def list_object_heights(
    snapshot: dict[str, Any], *, layer: str | None = None, type: str | None = None, limit: int = 50
) -> list[ObjectHeightRow]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIST_LIMIT}")
    names = queries._layer_names_by_id(snapshot)
    rows: list[ObjectHeightRow] = []
    for obj in snapshot.get("objects") or []:
        if layer is not None and not queries._layer_matches(obj, layer, names):
            continue
        if type is not None and not queries._type_matches(obj, type):
            continue
        info = _height_of(snapshot, obj)
        if info.height_cm is None:
            continue
        rows.append(ObjectHeightRow(
            item_id=info.item_id, name=info.name, object_type=info.object_type,
            height_cm=info.height_cm, source=info.source))
    rows.sort(key=lambda r: (-r.height_cm, r.name or "", r.item_id))
    return rows[:limit]


# --- whole-plan summary -----------------------------------------------------


def site_extents(snapshot: dict[str, Any]) -> SiteExtents:
    objects = [o for o in (snapshot.get("objects") or []) if o.get("type") != "background_image"]
    names = queries._layer_names_by_id(snapshot)
    counts: dict[str, int] = dict.fromkeys(names.values(), 0)
    boxes: list[tuple[float, float, float, float]] = []
    tallest: ObjectHeight | None = None
    for obj in objects:
        lid = str(obj.get("layer_id") or "")
        key = names.get(lid) or "(no layer)"
        counts[key] = counts.get(key, 0) + 1
        try:
            shape = sm.shape_from_object(obj)
        except ValueError:
            shape = None
        if shape is not None:
            boxes.append(shape.bbox)
        else:
            x, y, w, h = queries.object_bbox(obj)
            boxes.append((x, y, x + w, y + h))
        info = _height_of(snapshot, obj)
        if info.height_cm is not None and (tallest is None or info.height_cm > (tallest.height_cm or 0.0)):
            tallest = info
    width, height = queries._canvas_size(snapshot)
    out = SiteExtents(
        object_count=len(objects), canvas_width_cm=width, canvas_height_cm=height,
        layer_counts=counts, boundary=_boundary_summary(snapshot),
        tallest=None if tallest is None else ObjectHeightSummary(
            item_id=tallest.item_id, name=tallest.name, object_type=tallest.object_type,
            height_cm=tallest.height_cm or 0.0, source=tallest.source))
    if boxes:
        x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
        x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
        out.min_x_cm, out.min_y_cm, out.max_x_cm, out.max_y_cm = x0, y0, x1, y1
        out.width_cm, out.height_cm = x1 - x0, y1 - y0
        out.width_m, out.height_m = _m(x1 - x0), _m(y1 - y0)
    return out


def _boundary_summary(snapshot: dict[str, Any]) -> SiteBoundary | None:
    try:
        obj, shape, origin = _boundary_shape(snapshot)
    except ValueError:
        return None
    if shape is None or len(shape.parts) != 1 or shape.parts[0].kind == "point":
        return None
    part = shape.parts[0]
    implicit = part.kind == "line"
    ring = part.points
    area = part.area_cm2 if part.area_cm2 is not None else sm.ring_area(ring)
    area = max(area - sum(sm.ring_area(h) for h in part.holes), 0.0)
    perimeter = sm.part_perimeter(part) if not implicit else sm.polyline_length(ring, True)
    return SiteBoundary(
        item_id=None if obj is None else str(obj.get("item_id")),
        name=None if obj is None else obj.get("name"), origin=origin,
        area_cm2=area, area_m2=area / 1e4, perimeter_cm=perimeter, perimeter_m=_m(perimeter),
        closed_implicitly=implicit)
