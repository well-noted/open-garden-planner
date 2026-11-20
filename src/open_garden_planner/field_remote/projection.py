"""Phone-facing projections of the plan (design 1.6, 2.9). Pure Python: no Qt, no scene.

Everything here takes a *snapshot* (``ProjectManager.snapshot_dict``, a plain dict the host
fetched on the Qt thread) and returns JSON-ready data. Nothing reads the scene and nothing is
written anywhere, so a route can run these on the server's thread pool.

Frames: the plan is Y-up, centimetres. Geometry is sent in plan centimetres; the phone converts
lat/lon with the ``georef`` summary from ``/api/state`` or uses the ``x_cm``/``y_cm`` this module
returns for a position. ``GeoFrame`` is the only lat/lon <-> canvas conversion used.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import io
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from open_garden_planner.core.geo_frame import GeoFrame, GeoreferenceError, frame_from_snapshot
from open_garden_planner.core.georeference import meters_per_degree
from open_garden_planner.core.plan_north import north_angle_from_plan

MAX_SCENE_OBJECTS = 5000
MAX_OBJECT_POINTS = 200
#: Ramer-Douglas-Peucker tolerance for the phone's picture, plan centimetres (2 cm is far
#: below what a phone screen resolves on a garden).
SIMPLIFY_TOLERANCE_CM = 2.0
BASEMAP_MAX_PX = 2048
BASEMAP_JPEG_QUALITY = 80
#: A background this large is refused rather than decoded again on the server (the desktop
#: itself caps a loaded background at 120 MP).
BASEMAP_MAX_SOURCE_PX = 120_000_000
SPECIES_LIMIT = 25
MAX_BATCH = 50
CAPABILITIES = ("state", "scene", "basemap", "species", "field_records", "stream", "position")

FIX_QUALITIES = ("none", "gps", "dgps", "rtk_float", "rtk_fixed")
POSITION_SOURCES = ("browser", "nmea_usb", "nmea_ble", "native_fused")

#: Object types the phone does not need a picture of.
_SKIPPED_TYPES = frozenset({"background_image", "construction_line", "construction_circle", "text"})


class ProjectionError(ValueError):
    """A request the projection cannot serve; carries the HTTP status and an error code."""

    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail


def etag_of(payload: Any) -> str:
    """A strong ETag (quoted) over the canonical JSON of *payload*."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return '"' + hashlib.blake2b(raw, digest_size=12).hexdigest() + '"'


def etag_matches(header: str | None, etag: str) -> bool:
    """Whether an ``If-None-Match`` header names *etag* (weak validators and ``*`` count)."""
    if not header:
        return False
    if header.strip() == "*":
        return True
    candidates = (part.strip() for part in header.split(","))
    return any(c.removeprefix("W/") == etag for c in candidates)


def georef_of(snapshot: Mapping[str, Any]) -> GeoFrame | None:
    """The plan's GPS frame (core/georeference.py is the record), or None."""
    return frame_from_snapshot(snapshot)


# -- /api/state -----------------------------------------------------------------------------


def georef_summary(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """What the phone needs of the georeference: one similarity about the ENU origin.

    ``origin_lat``/``origin_lon`` is the GPS anchor and ``origin_x_cm``/``origin_y_cm`` where it
    sits on the canvas; ``rotation_deg`` and ``scale_meters_per_unit`` are measured through the
    real mapping (a calibration included), so the phone's own conversion in ``geo.js`` agrees
    with the desktop's for any similarity-like georeference.
    """
    north = north_angle_from_plan(snapshot.get("north_angle_deg"))
    geo = georef_of(snapshot)
    if geo is None:
        return {"georeferenced": False, "north_angle_deg": north}
    m_lat, m_lon = meters_per_degree(geo.origin_lat)
    ox, oy = geo.gps_to_canvas(geo.origin_lat, geo.origin_lon)
    return {
        "georeferenced": True,
        "origin_lat": geo.origin_lat,
        "origin_lon": geo.origin_lon,
        "origin_x_cm": ox,
        "origin_y_cm": oy,
        "scale_meters_per_unit": geo.scale_meters_per_unit,
        "rotation_deg": geo.rotation_deg,
        "m_per_deg": m_lat,  # lets the phone invert a plan point to lat/lon
        "m_per_deg_lon": m_lon,
        "north_angle_deg": north,
        "reference_point_count": len(geo.georeference.control_points),
        "fit_rms_m": geo.fit_rms_m,
        "source": geo.source,
    }


def plan_state(
    snapshot: Mapping[str, Any],
    *,
    plan_name: str | None,
    saved: bool,
    records_enabled: bool,
    server_time: str | None = None,
) -> tuple[dict[str, Any], str]:
    """``(body, etag)`` for ``GET /api/state``.

    The ETag covers everything except ``server_time``, so a phone polling every 5 s gets 304
    until the plan, a layer flag or a permission really changes.
    """
    canvas = snapshot.get("canvas") or {}
    layers = [
        {
            "id": str(layer.get("id", "")),
            "name": str(layer.get("name", "")),
            "visible": bool(layer.get("visible", True)),
            "locked": bool(layer.get("locked", False)),
        }
        for layer in (snapshot.get("layers") or [])
    ]
    stable = {
        "plan_name": plan_name or "",
        "saved": saved,
        "canvas": {"width_cm": _num(canvas.get("width")), "height_cm": _num(canvas.get("height"))},
        "georef": georef_summary(snapshot),
        "layers": layers,
        "has_basemap": _find_background(snapshot) is not None,
        "records_enabled": records_enabled,
        "capabilities": list(CAPABILITIES),
    }
    body = {**stable, "server_time": server_time or now_iso()}
    return body, etag_of(stable)


def now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _num(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


# -- /api/scene -----------------------------------------------------------------------------


def parse_bbox(text: str | None) -> tuple[float, float, float, float] | None:
    """``minx,miny,maxx,maxy`` in plan cm, or None when absent. Raises ``ProjectionError``."""
    if text is None or text == "":
        return None
    parts = text.split(",")
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        raise ProjectionError(400, "bad_bbox", "bbox must be four numbers: minx,miny,maxx,maxy.") from None
    if len(vals) != 4 or not all(math.isfinite(v) for v in vals):
        raise ProjectionError(400, "bad_bbox", "bbox must be four finite numbers: minx,miny,maxx,maxy.")
    min_x, min_y, max_x, max_y = vals
    if min_x > max_x or min_y > max_y:
        raise ProjectionError(400, "bad_bbox", "bbox must have minx <= maxx and miny <= maxy.")
    return min_x, min_y, max_x, max_y


def parse_layers(text: str | None) -> set[str] | None:
    """Layer ids or names, comma separated; None means all."""
    if text is None or text.strip() == "":
        return None
    names = {p.strip() for p in text.split(",") if p.strip()}
    if len(names) > 200:
        raise ProjectionError(400, "bad_layers", "Too many layers named.")
    return names


def _rdp(points: Sequence[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    """Ramer-Douglas-Peucker, iterative (no recursion limit on a 2000-vertex line)."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        ax, ay = points[first]
        bx, by = points[last]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        far_i, far_d = -1, 0.0
        for i in range(first + 1, last):
            px, py = points[i]
            dist = math.hypot(px - ax, py - ay) if length == 0 else abs(dx * (ay - py) - (ax - px) * dy) / length
            if dist > far_d:
                far_i, far_d = i, dist
        if far_i != -1 and far_d > tolerance:
            keep[far_i] = True
            stack.append((first, far_i))
            stack.append((far_i, last))
    return [p for p, k in zip(points, keep, strict=True) if k]


def simplify(points: Sequence[tuple[float, float]], *, closed: bool) -> list[tuple[float, float]]:
    """RDP at the default tolerance, then evenly thinned to ``MAX_OBJECT_POINTS``."""
    out = _rdp(points, SIMPLIFY_TOLERANCE_CM)
    floor = 3 if closed else 2
    if len(out) > MAX_OBJECT_POINTS:
        step = len(out) / MAX_OBJECT_POINTS
        picked = [out[int(i * step)] for i in range(MAX_OBJECT_POINTS - 1)] + [out[-1]]
        out = picked
    if len(out) < floor:
        out = list(points[:floor]) if len(points) >= floor else list(points)
    return out


def _round_pts(points: Iterable[tuple[float, float]]) -> list[list[float]]:
    return [[round(x, 1), round(y, 1)] for x, y in points]


def _rotate(points: Iterable[tuple[float, float]], cx: float, cy: float, deg: float) -> list[tuple[float, float]]:
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return [(cx + (x - cx) * c - (y - cy) * s, cy + (x - cx) * s + (y - cy) * c) for x, y in points]


def _bbox_of(points: Iterable[tuple[float, float]]) -> tuple[float, float, float, float]:
    pts = list(points)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _shape(obj: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[float, float, float, float]] | None:
    """The simplified geometry of one serialised object and its (minx, miny, maxx, maxy)."""
    from open_garden_planner.agent_api.queries import object_bbox

    kind = obj.get("type")
    rot = _num(obj.get("rotation_angle"))
    try:
        if kind == "rectangle" or ("width" in obj and "height" in obj and "x" in obj and "children" not in obj):
            x, y, w, h = _num(obj["x"]), _num(obj["y"]), _num(obj["width"]), _num(obj["height"])
            ring = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
            if rot:  # rotation about the rectangle's centre
                ring = _rotate(ring, x + w / 2, y + h / 2, rot)
            return {"kind": "polygon", "points": _round_pts(ring)}, _bbox_of(ring)
        if kind == "circle":
            cx, cy, r = _num(obj["center_x"]), _num(obj["center_y"]), _num(obj["radius"])
            return {"kind": "circle", "cx": round(cx, 1), "cy": round(cy, 1), "r": round(r, 1)}, (
                cx - r, cy - r, cx + r, cy + r,
            )
        if kind == "ellipse":
            cx, cy = _num(obj["center_x"]), _num(obj["center_y"])
            sx, sy = _num(obj["semi_x"]), _num(obj["semi_y"])
            geom = {"kind": "ellipse", "cx": round(cx, 1), "cy": round(cy, 1), "rx": round(sx, 1), "ry": round(sy, 1)}
            if rot:
                geom["rot_deg"] = round(rot, 2)
            reach = max(sx, sy) if rot else None
            box = (cx - reach, cy - reach, cx + reach, cy + reach) if reach is not None else (cx - sx, cy - sy, cx + sx, cy + sy)
            return geom, box
        if kind in ("polygon", "polyline") and obj.get("points"):
            raw = [(_num(p.get("x")), _num(p.get("y"))) for p in obj["points"] if isinstance(p, Mapping)]
            if len(raw) < 2:
                return None
            closed = kind == "polygon"
            pts = simplify(raw, closed=closed)
            if rot:
                cx, cy = (sum(p[0] for p in raw) / len(raw), sum(p[1] for p in raw) / len(raw))
                pts = _rotate(pts, cx, cy, rot)
            return {"kind": "polygon" if closed else "polyline", "points": _round_pts(pts)}, _bbox_of(pts)
        x, y, w, h = object_bbox(dict(obj))  # groups, bezier, arcs, pins: a box is enough
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return {"kind": "box", "x": round(x, 1), "y": round(y, 1), "w": round(w, 1), "h": round(h, 1)}, (
        x, y, x + w, y + h,
    )


def _species_name(obj: Mapping[str, Any]) -> str | None:
    value = obj.get("plant_species")
    if isinstance(value, str) and value:
        return value[:120]
    if isinstance(value, Mapping):
        for key in ("common_name", "scientific_name"):
            if isinstance(value.get(key), str) and value[key]:
                return str(value[key])[:120]
    return None


def project_scene(
    snapshot: Mapping[str, Any],
    *,
    bbox: tuple[float, float, float, float] | None = None,
    layers: set[str] | None = None,
    limit: int = MAX_SCENE_OBJECTS,
) -> tuple[dict[str, Any], str]:
    """``(body, etag)`` for ``GET /api/scene``: simplified geometry, plan cm, Y-up.

    Hidden layers are skipped (the phone shows what the desktop shows). ``truncated`` says the
    5000-object cap cut the answer; the phone should then ask for a smaller ``bbox``.
    """
    layer_rows = snapshot.get("layers") or []
    by_id = {str(layer.get("id")): layer for layer in layer_rows}
    wanted: set[str] | None = None
    if layers is not None:
        wanted = {
            str(layer.get("id")) for layer in layer_rows if str(layer.get("id")) in layers or layer.get("name") in layers
        }
    out: list[dict[str, Any]] = []
    matched = 0
    for obj in snapshot.get("objects") or []:
        if not isinstance(obj, Mapping) or obj.get("type") in _SKIPPED_TYPES:
            continue
        lid = str(obj.get("layer_id") or "")
        layer = by_id.get(lid)
        if layer is not None and not layer.get("visible", True):
            continue
        if wanted is not None and lid not in wanted:
            continue
        shaped = _shape(obj)
        if shaped is None:
            continue
        geom, (x0, y0, x1, y1) = shaped
        if bbox is not None and (x1 < bbox[0] or x0 > bbox[2] or y1 < bbox[1] or y0 > bbox[3]):
            continue
        matched += 1
        if len(out) >= limit:
            continue
        item: dict[str, Any] = {
            "id": str(obj.get("item_id", "")),
            "type": obj.get("object_type") or obj.get("type"),
            "layer_id": lid or None,
            **geom,
        }
        if obj.get("name"):
            item["name"] = str(obj["name"])[:120]
        species = _species_name(obj)
        if species:
            item["species"] = species
        for key in ("fill_color", "stroke_color"):
            if isinstance(obj.get(key), str):
                item[key] = obj[key]
        out.append(item)
    body = {
        "objects": out,
        "count": len(out),
        "matched": matched,
        "truncated": matched > len(out),
        "limit": limit,
        "units": "cm",
        "y_up": True,
    }
    return body, etag_of(body)


# -- /api/basemap ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Basemap:
    jpeg: bytes
    etag: str
    rect: dict[str, float]
    width_px: int
    height_px: int
    source_px: tuple[int, int]


def _find_background(snapshot: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for obj in snapshot.get("objects") or []:
        if isinstance(obj, Mapping) and obj.get("type") == "background_image" and obj.get("image_data"):
            return obj
    return None


def basemap_key(snapshot: Mapping[str, Any]) -> str | None:
    """A cheap identity of the background (data digest + placement), None without one.

    The host caches the JPEG under this key so the resize runs once per distinct background."""
    obj = _find_background(snapshot)
    if obj is None:
        return None
    digest = hashlib.blake2b(str(obj["image_data"]).encode("ascii", "ignore"), digest_size=12).hexdigest()
    pos = obj.get("position") or {}
    return f"{digest}:{pos.get('x')}:{pos.get('y')}:{obj.get('scale_factor')}"


def project_basemap(snapshot: Mapping[str, Any]) -> Basemap:
    """The background downscaled to at most 2048 px as a JPEG plus its plan rectangle.

    The rectangle is where the image lies on the plan (cm, Y-up): ``x_cm, y_cm`` is its
    bottom-left corner and the image's top row is at ``y_cm + height_cm``. Placement follows
    ``BackgroundImageItem``: the item scales about its centre and ``scale_factor`` is
    pixels per cm.
    """
    from PIL import Image

    obj = _find_background(snapshot)
    if obj is None:
        raise ProjectionError(404, "no_basemap", "The plan has no background image.")
    try:
        raw = base64.b64decode(str(obj["image_data"]), validate=False)
    except (binascii.Error, ValueError):
        raise ProjectionError(404, "no_basemap", "The background image could not be read.") from None
    try:
        with Image.open(io.BytesIO(raw)) as img:
            src_w, src_h = img.size
            if src_w * src_h > BASEMAP_MAX_SOURCE_PX:
                raise ProjectionError(413, "basemap_too_large", "The background image is too large to send.")
            img.draft("RGB", (BASEMAP_MAX_PX, BASEMAP_MAX_PX))
            work = img.convert("RGB")
    except ProjectionError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError):
        raise ProjectionError(404, "no_basemap", "The background image could not be decoded.") from None
    work.thumbnail((BASEMAP_MAX_PX, BASEMAP_MAX_PX), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    work.save(buffer, "JPEG", quality=BASEMAP_JPEG_QUALITY, optimize=True)
    pos = obj.get("position") or {}
    factor = _num(obj.get("scale_factor")) or 1.0  # pixels per cm
    cm_per_px = 1.0 / factor
    w_cm, h_cm = src_w * cm_per_px, src_h * cm_per_px
    # The item's scene centre is pos + (w/2, h/2) in source pixels (it scales about the centre).
    cx, cy = _num(pos.get("x")) + src_w / 2, _num(pos.get("y")) + src_h / 2
    rect = {
        "x_cm": round(cx - w_cm / 2, 2),
        "y_cm": round(cy - h_cm / 2, 2),
        "width_cm": round(w_cm, 2),
        "height_cm": round(h_cm, 2),
    }
    jpeg = buffer.getvalue()
    etag = '"' + hashlib.blake2b(jpeg + json.dumps(rect, sort_keys=True).encode(), digest_size=12).hexdigest() + '"'
    return Basemap(jpeg, etag, rect, work.width, work.height, (src_w, src_h))


# -- /api/species ---------------------------------------------------------------------------


def species_rows(query: str | None, *, limit: int = SPECIES_LIMIT) -> list[dict[str, Any]]:
    """Bundled and custom species whose names match *query*, at most *limit* rows."""
    from open_garden_planner.services.bundled_species_db import get_species_db
    from open_garden_planner.services.permaculture import matches_traits
    from open_garden_planner.services.species_catalog import offline_catalog

    text = (query or "").strip()[:80]
    bundled = get_species_db()
    rows = []
    for key, rec in offline_catalog().items():
        if not matches_traits(rec, text):
            continue
        rows.append(
            {
                "species_key": key,
                "common_name": str(rec.get("common_name") or ""),
                "scientific_name": str(rec.get("scientific_name") or ""),
                "family": str(rec.get("family") or ""),
                "permaculture_layer": str(rec.get("permaculture_layer") or "unknown"),
                "source": "bundled" if key in bundled else "custom",
            }
        )
    rows.sort(key=lambda r: (r["common_name"].casefold(), r["species_key"]))
    return rows[:limit]


# -- /api/position --------------------------------------------------------------------------


@dataclass(frozen=True)
class Fix:
    """One validated position report (design 1.6)."""

    t: float
    lat: float
    lon: float
    accuracy_m: float
    alt: float | None = None
    heading_deg: float | None = None
    speed_mps: float | None = None
    fix_quality: str | None = None
    hdop: float | None = None
    sigma_m: float | None = None
    source: str = "browser"


#: NMEA GGA meaning mapped onto the public fix modes (design 2.9). ``dgps`` is still a
#: standard-precision fix for the purposes of any claim.
_QUALITY_TO_MODE = {"rtk_fixed": "rtk_fixed", "rtk_float": "rtk_float", "dgps": "standard", "gps": "standard"}


def canvas_heading_deg(heading_deg: float, rotation_deg: float) -> float:
    """A compass heading (clockwise from true north) as a plan angle (counter-clockwise from +x, Y-up).

    The georeference maps east/north to canvas with ``x = e*cos(a) + n*sin(a)``,
    ``y = -e*sin(a) + n*cos(a)`` (``a`` = the plan's north angle); the heading's direction is
    ``(e, n) = (sin h, cos h)``. Plan north (``a`` = 0) therefore reads 90, east reads 0.
    """
    h, a = math.radians(heading_deg), math.radians(rotation_deg)
    e, n = math.sin(h), math.cos(h)
    dx = e * math.cos(a) + n * math.sin(a)
    dy = -e * math.sin(a) + n * math.cos(a)
    return math.degrees(math.atan2(dy, dx)) % 360.0


def telemetry(
    fixes: Sequence[Fix],
    geo: GeoFrame | None,
    *,
    solver: Any = None,
) -> dict[str, Any]:
    """Telemetry for a position batch (design 2.9); the latest fix is the answer.

    TODO(WP-B wiring): *solver* is the hook for ``core.positioning.engine`` (Kalman, gating,
    ZUPT, calibration). While it is absent this returns the raw projected position with
    ``fix_mode`` ``standard`` (or the client-reported quality), ``sigma_m`` equal to the
    reported accuracy and ``gated`` false. No smoothing is claimed. A solver is any callable
    ``(fixes, geo) -> dict`` returning the same keys.
    """
    if not fixes:
        raise ProjectionError(422, "invalid_position", "No position in the request.")
    if solver is not None:
        return dict(solver(fixes, geo))
    last = fixes[-1]
    x_cm = y_cm = None
    if geo is not None:
        try:
            x_cm, y_cm = geo.gps_to_canvas(last.lat, last.lon)
        except GeoreferenceError:
            x_cm = y_cm = None
    sigma = last.sigma_m if last.sigma_m is not None else last.accuracy_m
    reported = last.fix_quality is not None and last.fix_quality != "none"
    mode = _QUALITY_TO_MODE.get(last.fix_quality or "", "standard")
    quality = "good" if sigma <= 3.0 else "fair" if sigma <= 10.0 else "poor"
    heading_source = "gnss" if last.heading_deg is not None else "none"
    heading_canvas = None
    if geo is not None and last.heading_deg is not None:
        heading_canvas = round(canvas_heading_deg(last.heading_deg, geo.rotation_deg), 2)
    return {
        "t": last.t,
        "lat": last.lat,
        "lon": last.lon,
        "x_cm": None if x_cm is None else round(x_cm, 1),
        "y_cm": None if y_cm is None else round(y_cm, 1),
        "sigma_m": round(sigma, 2),
        "horizontal_accuracy_m": round(last.accuracy_m, 2),
        "heading_deg": last.heading_deg,
        "heading_source": heading_source,
        "heading_canvas_deg": heading_canvas,
        "speed_mps": last.speed_mps,
        "fix_mode": mode,
        "fix_mode_reported": reported,
        "fix_source": last.source,
        "anchor_source": "none",
        "anchor_age_s": None,
        "stationary": False,
        "gated": False,
        "quality": quality,
        "samples_used": len(fixes),
        "georeferenced": geo is not None,
    }
