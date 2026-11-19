"""Qt-free planner: a reviewed field record becomes one thing to put on the plan (WP-R).

``plan_record`` is a pure function of a :class:`~services.field_records.FieldRecord`, the
plan's georeference and the species database. It never touches the scene, the sidecar or
a command; it returns a *spec* that the desktop approver turns into the plan's own
undoable commands:

* :class:`ObjectSpec` - one ``create_object`` call (a plant, or a callout for a note, a
  measurement or a placement request that names no known species);
* :class:`PolylineSpec` - an irrigation line, drawn on the "Irrigation" layer;
* :class:`ReferencePointSpec` - a survey correspondence for the georeference fit dialog.

Where a point goes: the plan x/y **captured with the record** (design 1.9) when it has
them, so a later georeference change does not silently move an approved record; otherwise
the record's GPS fix through the georeference, with the camera/bearing rule that
``place_geotagged_feature`` always had. Everything that can refuse raises
:class:`PlacementError` before any command exists.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from open_garden_planner.core.georef import GeoReference, GeorefError
from open_garden_planner.core.plant_sizing import species_drop_diameter_cm
from open_garden_planner.services import field_records as fr
from open_garden_planner.services import photo_geotag as pg

#: Furthest a record may be placed from the camera along its bearing, metres.
MAX_DISTANCE_M = 500.0
#: The layer irrigation lines go on. Created on approval, never unlocked.
IRRIGATION_LAYER_NAME = "Irrigation"
#: Kinds that become a plant when a species can be matched.
PLANT_LIKE_KINDS = frozenset({"plant", "placement_request"})
#: The object types a species can be placed as.
PLANT_TYPES = frozenset({"TREE", "SHRUB", "PERENNIAL"})
#: Statuses a record may be approved from. A ``placed`` record needs ``allow_duplicate``.
APPROVABLE_STATUSES = frozenset({"pending", "approved"})

SpeciesLookup = Callable[[str], "Mapping[str, Any] | None"]


class PlacementError(ValueError):
    """The record cannot be placed (the message tells the reviewer why)."""


@dataclass(frozen=True)
class ObjectSpec:
    """One object for ``create_object`` (scene cm, Y-up)."""

    object_type: str
    x: float
    y: float
    radius: float | None = None
    name: str | None = None
    species: str | None = None
    text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    true_bearing_deg: float | None = None


@dataclass(frozen=True)
class PolylineSpec:
    """An open polyline for the Irrigation layer (scene cm)."""

    points: tuple[tuple[float, float], ...]
    object_type: str = "PATH"
    layer_name: str = IRRIGATION_LAYER_NAME
    name: str | None = None
    stroke_width_cm: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReferencePointSpec:
    """A survey correspondence: the GPS fix and the plan point the user says it is."""

    name: str
    lat: float
    lon: float
    x: float
    y: float


@dataclass(frozen=True)
class Placement:
    """What approving one record would do."""

    record_id: str
    spec: ObjectSpec | PolylineSpec | ReferencePointSpec
    warnings: tuple[str, ...] = ()


# -- shared geometry helpers (also used by the agent's place_geotagged_feature) ------------


def finite_in(value: object, lo: float, hi: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and lo <= value <= hi
    )


def distance_cm(distance_m: object, geo: GeoReference | None) -> float:
    if not finite_in(distance_m, 0.0, MAX_DISTANCE_M):
        raise ValueError(f"distance_m must be between 0 and {MAX_DISTANCE_M:g} metres")
    assert isinstance(distance_m, (int, float))
    scale = geo.scale_meters_per_unit if geo is not None else 0.01
    return float(distance_m) / scale


def object_type_for_species(species: Mapping[str, Any]) -> str:
    """TREE / SHRUB / PERENNIAL from the species' permaculture layer and height."""
    layer = str(species.get("permaculture_layer") or "")
    height = species.get("max_height_cm")
    tall = isinstance(height, (int, float)) and height >= 400
    if layer in ("canopy", "sub_canopy") or tall:
        return "TREE"
    if layer in ("shrub", "vine"):
        return "SHRUB"
    return "PERENNIAL"


def mature_radius_cm(species: Mapping[str, Any], object_type: str) -> float:
    """Mature canopy radius (half the species' max spread, else the type default)."""
    return species_drop_diameter_cm(dict(species), object_type) / 2.0


def resolve_target(
    geo: GeoReference,
    record: fr.FieldRecord | None,
    *,
    lat: float | None,
    lon: float | None,
    bearing_deg: float | None,
    distance_m: float | None,
) -> dict[str, Any]:
    """Where the feature goes on the canvas.

    Camera position = explicit ``lat``/``lon`` else the record's. With a ``distance_m`` the
    feature sits that far from the camera along the bearing (explicit ``bearing_deg`` else
    the record's, TRUE north); without it, at the camera. Returns ``x``/``y`` (scene cm), the
    camera and the true bearing used.
    """
    cam_lat = lat if lat is not None else (record.lat if record else None)
    cam_lon = lon if lon is not None else (record.lon if record else None)
    if cam_lat is None or cam_lon is None:
        raise ValueError("no position: give lat and lon, or a record_id whose record has a GPS fix")
    try:
        cx, cy = geo.gps_to_canvas(cam_lat, cam_lon)
    except GeorefError as error:
        raise ValueError(f"the position cannot be placed on this plan: {error}") from error
    bearing = bearing_deg if bearing_deg is not None else (record.bearing_deg if record else None)
    if bearing is not None and not finite_in(bearing, 0.0, 360.0):
        raise ValueError("bearing_deg must be between 0 and 360")
    x, y = cx, cy
    if distance_m is not None and distance_m > 0:
        if bearing is None:
            raise ValueError("distance_m needs a bearing: give bearing_deg or use a record that has one")
        dist_cm = distance_cm(distance_m, geo)
        dx, dy = pg.canvas_direction(bearing, geo.rotation_deg)
        x, y = cx + dx * dist_cm, cy + dy * dist_cm
    elif distance_m is not None:
        distance_cm(distance_m, geo)  # validates (0 is allowed: at the camera)
    return {
        "x": x,
        "y": y,
        "camera_x_cm": cx,
        "camera_y_cm": cy,
        "true_bearing_deg": None if bearing is None else bearing % 360.0,
    }


def field_metadata(
    record: fr.FieldRecord | None,
    target: Mapping[str, Any],
    *,
    plan_path: str | None,
    distance_m: float | None,
    lat: float | None,
    lon: float | None,
) -> dict[str, Any]:
    """The ``metadata['field_record']`` block stored on a placed object."""
    block: dict[str, Any] = {
        "camera_lat": lat if lat is not None else (record.lat if record else None),
        "camera_lon": lon if lon is not None else (record.lon if record else None),
        "bearing_deg": target.get("true_bearing_deg"),
        "distance_m": distance_m,
    }
    if record is not None:
        block.update(
            {
                "record_id": record.id,
                "photo": record.photo,
                "photo_plan": Path(plan_path).name if plan_path else None,
                "age": record.age,
                "notes": record.notes,
                "fix_mode": record.fix_mode,
                "accuracy_m": record.accuracy_m,
                "observed_at": record.timestamp,
            }
        )
    return {k: v for k, v in block.items() if v is not None}


# -- the planner ----------------------------------------------------------------------------


def check_approvable(record: fr.FieldRecord, *, allow_duplicate: bool = False) -> None:
    """Refuse a record that is already decided (one rule for the panel and the agent)."""
    if record.status == "placed" or record.placed_object_id:
        if not allow_duplicate:
            raise PlacementError(
                f"Record {record.id} is already placed as object {record.placed_object_id}; "
                "reopen it first (or pass allow_duplicate=true to place it again)."
            )
        return
    if record.status not in APPROVABLE_STATUSES:
        raise PlacementError(f"Record {record.id} is {record.status}; reopen it before placing it.")


def _vertex_xy(vertex: list[Any], geo: GeoReference | None) -> tuple[float, float]:
    lat, lon, x, y = (vertex + [None] * 4)[:4]
    if x is not None and y is not None:
        return float(x), float(y)
    if lat is None or lon is None:
        raise PlacementError("a vertex has no position")
    if geo is None:
        raise PlacementError("The plan is not georeferenced; set it up before placing this record.")
    try:
        return geo.gps_to_canvas(lat, lon)
    except GeorefError as error:
        raise PlacementError(f"a vertex cannot be placed on this plan: {error}") from error


def _point_xy(record: fr.FieldRecord, geo: GeoReference | None) -> tuple[float, float, float | None]:
    """Plan position of a point record and the true bearing, if any."""
    geometry = record.geometry
    if geometry and geometry.get("type") == "point":
        x, y = geometry.get("x_cm"), geometry.get("y_cm")
        if x is not None and y is not None:
            return float(x), float(y), None if record.bearing_deg is None else record.bearing_deg % 360.0
    if geo is None:
        raise PlacementError(
            "The plan is not georeferenced; set it up first (set_initial_map, set_georeference or the "
            "Georeference dialog)."
        )
    try:
        target = resolve_target(
            geo, record, lat=None, lon=None, bearing_deg=None, distance_m=None
        )
    except ValueError as error:
        raise PlacementError(str(error)) from error
    return float(target["x"]), float(target["y"]), target["true_bearing_deg"]


def _record_block(record: fr.FieldRecord, plan_path: str | None, bearing: float | None) -> dict[str, Any]:
    block = field_metadata(
        record,
        {"true_bearing_deg": bearing},
        plan_path=plan_path,
        distance_m=None,
        lat=None,
        lon=None,
    )
    block["kind"] = record.kind
    for key in ("what", "requester", "group_id", "precision_hint", "sigma_m", "name"):
        value = getattr(record, key)
        if value is not None:
            block[key] = value
    return block


def _callout_text(record: fr.FieldRecord) -> str:
    parts = [p for p in (record.what, record.name, record.plant_name, record.notes) if p]
    if record.kind == "measurement" and record.rms is not None:
        parts.append(f"rms {record.rms:g}")
    text = " - ".join(dict.fromkeys(parts)).strip()
    return text or record.kind.replace("_", " ")


def plan_record(
    record: fr.FieldRecord,
    geo: GeoReference | None,
    *,
    plan_path: str | None = None,
    species_lookup: SpeciesLookup | None = None,
    object_type: str | None = None,
    allow_duplicate: bool = False,
) -> Placement:
    """Plan the approval of one record or raise :class:`PlacementError`. Pure."""
    check_approvable(record, allow_duplicate=allow_duplicate)
    kind = record.kind
    if kind == "reference_point":
        return _plan_reference_point(record)
    if kind == "irrigation_path":
        return _plan_irrigation(record, geo, plan_path)
    x, y, bearing = _point_xy(record, geo)
    block = _record_block(record, plan_path, bearing)
    warnings: list[str] = []
    if kind in PLANT_LIKE_KINDS:
        wanted = record.species_key or record.plant_name or (record.what if kind == "placement_request" else None)
        matched = _lookup(wanted, species_lookup)
        if matched is not None:
            return Placement(record.id, _plant_spec(record, matched, wanted or "", x, y, bearing, block, object_type))
        if kind == "plant":
            if not wanted:
                raise PlacementError("no species: the record has neither a species_key nor a plant_name")
            raise PlacementError(
                f"No species named {wanted!r} in the bundled database. Give the record a species_key "
                "from the species list first."
            )
        warnings.append("No known species in the request; placed as a note.")
    return Placement(
        record.id,
        ObjectSpec(
            "GENERIC_CALLOUT",
            x,
            y,
            text=_callout_text(record),
            metadata={"field_record": block},
            true_bearing_deg=bearing,
        ),
        tuple(warnings),
    )


def _lookup(wanted: str | None, species_lookup: SpeciesLookup | None) -> Mapping[str, Any] | None:
    if not wanted:
        return None
    if species_lookup is None:
        from open_garden_planner.services.bundled_species_db import lookup_species

        species_lookup = lookup_species
    return species_lookup(wanted)


def _plant_spec(
    record: fr.FieldRecord,
    matched: Mapping[str, Any],
    wanted: str,
    x: float,
    y: float,
    bearing: float | None,
    block: dict[str, Any],
    object_type: str | None,
) -> ObjectSpec:
    species = dict(matched)
    resolved = object_type or object_type_for_species(species)
    if resolved not in PLANT_TYPES:
        raise PlacementError("object_type must be TREE, SHRUB or PERENNIAL")
    display = record.plant_name or species.get("common_name")
    return ObjectSpec(
        resolved,
        x,
        y,
        radius=mature_radius_cm(species, resolved),
        name=display if isinstance(display, str) else None,
        species=str(species.get("scientific_name") or wanted),
        metadata={"field_record": block},
        true_bearing_deg=bearing,
    )


def _plan_reference_point(record: fr.FieldRecord) -> Placement:
    geometry = record.geometry or {}
    if record.lat is None or record.lon is None:
        raise PlacementError("A reference point needs the GPS fix it was taken at.")
    x, y = geometry.get("x_cm"), geometry.get("y_cm")
    if x is None or y is None:
        raise PlacementError("A reference point needs the plan position the user says it is (x_cm, y_cm).")
    name = record.name or record.plant_name or f"Field point {record.id[:6]}"
    return Placement(record.id, ReferencePointSpec(name, record.lat, record.lon, float(x), float(y)))


def _plan_irrigation(record: fr.FieldRecord, geo: GeoReference | None, plan_path: str | None) -> Placement:
    geometry = record.geometry
    if not geometry or geometry.get("type") != "polyline":
        raise PlacementError("An irrigation record needs a polyline geometry.")
    points = [_vertex_xy(list(v), geo) for v in geometry["points"]]
    if len(points) < 2:
        raise PlacementError("An irrigation line needs at least two vertices.")
    if record.closes_on and len(points) >= 3 and points[0] != points[-1]:
        points.append(points[0])
    if len({(round(px, 3), round(py, 3)) for px, py in points}) < 2:
        raise PlacementError("All vertices are at the same position.")
    block = _record_block(record, plan_path, None)
    for key in ("diameter_mm", "flow_lph", "emitter_spacing_cm", "closes_on"):
        value = getattr(record, key)
        if value is not None:
            block[key] = value
    width = None if record.diameter_mm is None else max(1.0, record.diameter_mm / 10.0)
    return Placement(
        record.id,
        PolylineSpec(
            tuple(points),
            name=record.name or record.plant_name or "Irrigation line",
            stroke_width_cm=width,
            metadata={"field_record": block},
        ),
    )
