"""Qt-free helpers behind the Agent API field-photo tools (ADR-080).

Pure functions over the photo EXIF reader (:mod:`services.photo_geotag`), the
sidecar store (:mod:`services.field_records`) and a plan's georeference. The
server, the app host and the tests share them; the host only adds the Qt-thread
hop and the one undoable command of ``place_geotagged_feature``.
"""

from __future__ import annotations

import contextlib
from typing import Any

from open_garden_planner.core.georef import GeoReference, GeorefError
from open_garden_planner.services import field_records as fr
from open_garden_planner.services import photo_geotag as pg
from open_garden_planner.services.field_record_placement import (  # noqa: F401  (re-exported API)
    MAX_DISTANCE_M,
    field_metadata,
    mature_radius_cm,
    object_type_for_species,
    resolve_target,
)
from open_garden_planner.services.field_record_placement import (
    distance_cm as _distance_cm,
)
from open_garden_planner.services.field_record_placement import (
    finite_in as _finite_in,
)

MAX_LIST = 500
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
    status: str | None = None,
    kind: str | None = None,
    group_id: str | None = None,
) -> dict[str, Any]:
    """The ``list_field_records`` payload (newest first)."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST:
        raise ValueError(f"limit must be an integer between 1 and {MAX_LIST}")
    if status is not None and status not in fr.STATUSES:
        raise ValueError("status must be one of " + ", ".join(sorted(fr.STATUSES)))
    if kind is not None and kind not in fr.KINDS:
        raise ValueError("kind must be one of " + ", ".join(sorted(fr.KINDS)))
    loaded = fr.load_records(plan_path)
    # EXIF capture times carry no zone, ingest stamps are UTC 'Z': compare the first 19
    # characters (naive times are read as UTC) so mixed records sort consistently.
    recs = sorted(loaded.records, key=lambda r: r.timestamp[:19], reverse=True)
    if only_unplaced:
        recs = [r for r in recs if not r.placed_object_id and r.status in ("pending", "approved")]
    recs = [
        r
        for r in recs
        if (status is None or r.status == status)
        and (kind is None or r.kind == kind)
        and (group_id is None or r.group_id == group_id)
    ]
    shown = recs[:limit]
    groups: dict[str, list[fr.FieldRecord]] = {}
    for rec in shown:
        if rec.group_id:
            groups.setdefault(rec.group_id, []).append(rec)
    return {
        "total": len(loaded.records),
        "returned": len(shown),
        "records": [record_view(r, plan_path, geo) for r in shown],
        "groups": [
            {
                "group_id": gid,
                "what": members[0].what,
                "record_ids": [m.id for m in members],
                "open_ids": [m.id for m in members if m.status in ("pending", "approved")],
            }
            for gid, members in groups.items()
        ],
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
            "source": "agent",
        },
        raw,
    )
    return {
        "record": record_view(record, plan_path, geo),
        "geotag": _geotag_payload(tag, geo, magnetic_declination_deg, None),
        "warnings": warnings,
    }
