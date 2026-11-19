"""Qt-free helpers behind the Agent API field-photo tools (ADR-080).

Pure functions over the photo EXIF reader (:mod:`services.photo_geotag`), the
sidecar store (:mod:`services.field_records`) and a plan's georeference. The
server, the app host and the tests share them; the host only adds the Qt-thread
hop and the one undoable command of ``place_geotagged_feature``.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from open_garden_planner.core.georef import GeoReference, GeorefError
from open_garden_planner.core.plant_sizing import species_drop_diameter_cm
from open_garden_planner.services import field_records as fr
from open_garden_planner.services import photo_geotag as pg

MAX_LIST = 500
#: Furthest a record may be placed from the camera along its bearing, metres.
MAX_DISTANCE_M = 500.0


def extract_geotag(
    geo: GeoReference | None,
    *,
    path: str | None = None,
    data_base64: str | None = None,
    magnetic_declination_deg: float | None = None,
    distance_m: float | None = None,
) -> dict[str, Any]:
    """The ``extract_photo_geotag`` payload: EXIF fields + the camera on the plan."""
    tag = pg.parse_photo(path=path, data_base64=data_base64)
    return _geotag_payload(tag, geo, magnetic_declination_deg, distance_m)


def _geotag_payload(
    tag: pg.PhotoGeotag,
    geo: GeoReference | None,
    magnetic_declination_deg: float | None,
    distance_m: float | None,
) -> dict[str, Any]:
    if magnetic_declination_deg is not None and not _finite_in(magnetic_declination_deg, -90, 90):
        raise ValueError("magnetic_declination_deg must be between -90 and 90")
    distance_cm = None if distance_m is None else _distance_cm(distance_m, geo)
    projected = pg.project_to_plan(
        tag, geo, magnetic_declination_deg=magnetic_declination_deg, distance_cm=distance_cm
    )
    return {**{k: v for k, v in tag.to_dict().items() if k != "warnings"}, **projected}


def _finite_in(value: object, lo: float, hi: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and lo <= value <= hi
    )


def _distance_cm(distance_m: object, geo: GeoReference | None) -> float:
    if not _finite_in(distance_m, 0.0, MAX_DISTANCE_M):
        raise ValueError(f"distance_m must be between 0 and {MAX_DISTANCE_M:g} metres")
    assert isinstance(distance_m, (int, float))
    scale = geo.scale_meters_per_unit if geo is not None else 0.01
    return float(distance_m) / scale


def record_view(
    rec: fr.FieldRecord, plan_path: str | None, geo: GeoReference | None
) -> dict[str, Any]:
    """A record as the agent sees it: its fields, its absolute photo path and canvas position."""
    out = rec.to_dict()
    out["photo_path"] = None
    out["canvas_x_cm"] = out["canvas_y_cm"] = None
    if rec.photo:
        try:
            out["photo_path"] = str(fr.resolve_photo(plan_path, rec))
        except fr.FieldRecordError:
            out["photo_path"] = None
    if geo is not None and rec.lat is not None and rec.lon is not None:
        with contextlib.suppress(GeorefError):
            out["canvas_x_cm"], out["canvas_y_cm"] = geo.gps_to_canvas(rec.lat, rec.lon)
    return out


def list_records(
    plan_path: str | None,
    geo: GeoReference | None,
    *,
    only_unplaced: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    """The ``list_field_records`` payload (newest first)."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST:
        raise ValueError(f"limit must be an integer between 1 and {MAX_LIST}")
    loaded = fr.load_records(plan_path)
    # EXIF capture times carry no zone, ingest stamps are UTC 'Z': compare the first 19
    # characters (naive times are read as UTC) so mixed records sort consistently.
    recs = sorted(loaded.records, key=lambda r: r.timestamp[:19], reverse=True)
    if only_unplaced:
        recs = [r for r in recs if not r.placed_object_id]
    return {
        "total": len(loaded.records),
        "returned": min(len(recs), limit),
        "records": [record_view(r, plan_path, geo) for r in recs[:limit]],
        "warnings": loaded.warnings,
    }


def ingest(
    plan_path: str | None,
    geo: GeoReference | None,
    *,
    path: str | None = None,
    data_base64: str | None = None,
    plant_name: str | None = None,
    species_key: str | None = None,
    age: str | None = None,
    notes: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    accuracy_m: float | None = None,
    bearing_deg: float | None = None,
    fix_mode: str | None = None,
    magnetic_declination_deg: float | None = None,
) -> dict[str, Any]:
    """The ``ingest_site_photo`` payload: store the photo and make a record.

    Explicit ``lat``/``lon``/``bearing_deg`` win over the photo's EXIF; anything
    missing is filled from the EXIF when present. Refuses a plan that was never
    saved (the sidecar needs a path) BEFORE touching anything.
    """
    fr.sidecar_paths(plan_path)  # unsaved plan -> refuse early
    if magnetic_declination_deg is not None and not _finite_in(magnetic_declination_deg, -90, 90):
        raise ValueError("magnetic_declination_deg must be between -90 and 90")
    for label, value in (("accuracy_m", accuracy_m), ("bearing_deg", bearing_deg)):
        if value is not None and not _finite_in(value, 0.0, 100000.0 if label == "accuracy_m" else 360.0):
            raise ValueError(f"{label} is out of range")
    raw = pg.photo_bytes_from_source(path=path, data_base64=data_base64)
    tag = pg.parse_photo_exif(raw)
    warnings = list(tag.warnings)
    used_exif_pos = False
    if lat is None and lon is None and tag.has_position:
        lat, lon, used_exif_pos = tag.lat, tag.lon, True
    if bearing_deg is None and tag.bearing_deg is not None:
        bearing_deg = tag.bearing_deg
        if tag.bearing_reference == "magnetic":
            if magnetic_declination_deg is None:
                warnings.append("magnetic bearing stored as given (no declination)")
            else:
                bearing_deg = (bearing_deg + magnetic_declination_deg) % 360.0
    if fix_mode is None and lat is not None:
        fix_mode = "exif" if used_exif_pos else "manual"
    if accuracy_m is None and used_exif_pos:
        accuracy_m = tag.gps_accuracy_m
    record = fr.add_record(
        plan_path,
        {
            "plant_name": plant_name,
            "species_key": species_key,
            "age": age,
            "notes": notes,
            "lat": lat,
            "lon": lon,
            "accuracy_m": accuracy_m,
            "bearing_deg": bearing_deg,
            "fix_mode": fix_mode,
            "timestamp": tag.taken_at,
        },
        raw,
    )
    return {
        "record": record_view(record, plan_path, geo),
        "geotag": _geotag_payload(tag, geo, magnetic_declination_deg, None),
        "warnings": warnings,
    }


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

    Camera position = explicit ``lat``/``lon`` else the record's. With a
    ``distance_m`` the feature sits that far from the camera along the bearing
    (explicit ``bearing_deg`` else the record's, TRUE north); without it, at the
    camera. Returns ``x``/``y`` (scene cm), the camera and the true bearing used.
    """
    cam_lat = lat if lat is not None else (record.lat if record else None)
    cam_lon = lon if lon is not None else (record.lon if record else None)
    if cam_lat is None or cam_lon is None:
        raise ValueError(
            "no position: give lat and lon, or a record_id whose record has a GPS fix"
        )
    try:
        cx, cy = geo.gps_to_canvas(cam_lat, cam_lon)
    except GeorefError as error:
        raise ValueError(f"the position cannot be placed on this plan: {error}") from error
    bearing = bearing_deg if bearing_deg is not None else (record.bearing_deg if record else None)
    if bearing is not None and not _finite_in(bearing, 0.0, 360.0):
        raise ValueError("bearing_deg must be between 0 and 360")
    x, y = cx, cy
    if distance_m is not None and distance_m > 0:
        if bearing is None:
            raise ValueError("distance_m needs a bearing: give bearing_deg or use a record that has one")
        dist_cm = _distance_cm(distance_m, geo)
        dx, dy = pg.canvas_direction(bearing, geo.rotation_deg)
        x, y = cx + dx * dist_cm, cy + dy * dist_cm
    elif distance_m is not None:
        _distance_cm(distance_m, geo)  # validates (0 is allowed: at the camera)
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
