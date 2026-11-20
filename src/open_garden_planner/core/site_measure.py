"""Pure site-measurement geometry shared by every Agent API measure tool.

Qt-free, no I/O. It takes the ``.ogp``-shaped object dicts the Agent API
snapshot carries and answers area, length, clearance, point-to-object, path and
bearing questions in scene centimetres (CAD Y-up, ADR-002; ADR-078 keeps
geometry in cm). ONE geometry core serves ``measure_area``, ``measure_length``,
``measure_clearance``, ``measure_to_object``, ``measure_points``,
``measure_path`` and ``site_extents``: every object becomes a :class:`Shape`
(parts of rings, open lines and points, in scene coordinates AFTER rotation) and
every distance is a segment distance over the shapes' edges.

Rotation follows the serialiser (``core/project._serialize_item_core``): a
rectangle, ellipse or polygon is stored un-rotated about its own bounding-box
centre together with ``rotation_angle`` (degrees, the Qt convention
``x' = cx + dx*cos - dy*sin``, ``y' = cy + dx*sin + dy*cos``). Group children
live in the group's local frame offset by the group's ``x``/``y``.

Curves: circles and ellipses are measured on a 360-gon inscribed in the curve
(clearance error below 4e-5 of the radius); their area and perimeter use the
closed forms the canvas uses (``core/measurements``). Bezier segments are
flattened to 64 chords each; arcs use the exact arc length.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

Pt = tuple[float, float]

#: Coordinates beyond +-100 km are a mistake or an attack, not a garden.
MAX_COORD_CM = 1e7
#: A measured path is a handful of legs; the cap bounds work and response size.
MAX_PATH_POINTS = 200
#: Vertices of the inscribed polygon standing in for a circle or ellipse.
CURVE_SEGMENTS = 360
#: Chords per cubic bezier segment.
BEZIER_STEPS = 64
#: Below this (cm) two shapes are said to touch.
TOUCH_TOLERANCE_CM = 1e-6
#: Mean Earth radius (IUGG), metres, as ``core.georef`` uses.
EARTH_RADIUS_M = 6371008.8


# --- validation -------------------------------------------------------------


def finite_cm(value: object, name: str) -> float:
    """``value`` as a finite float within +-``MAX_COORD_CM``, else ``ValueError``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, not {type(value).__name__}")
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} is not finite") from error
    if not math.isfinite(number):
        raise ValueError(f"{name} is not finite (NaN/inf are refused)")
    if abs(number) > MAX_COORD_CM:
        raise ValueError(
            f"{name}={number:g} is beyond +-{MAX_COORD_CM:g} cm (100 km) from the plan origin"
        )
    return number


# --- shapes -----------------------------------------------------------------


@dataclass(frozen=True)
class Part:
    """One connected piece: a closed ring (with holes), an open line, or a point."""

    kind: str  # "ring" | "line" | "point"
    points: tuple[Pt, ...]
    holes: tuple[tuple[Pt, ...], ...] = ()
    area_cm2: float | None = None  # exact closed form when the points are a sampling
    length_cm: float | None = None  # exact perimeter / arc length when sampled
    curved: bool = False


@dataclass(frozen=True)
class Shape:
    geometry: str
    parts: tuple[Part, ...]
    vertex_count: int | None = None
    _bbox: tuple[float, float, float, float] = field(default=(0.0, 0.0, 0.0, 0.0), compare=False)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """``(min_x, min_y, max_x, max_y)`` in scene cm."""
        return self._bbox


def _make_shape(geometry: str, parts: Sequence[Part], vertex_count: int | None) -> Shape:
    xs = [p[0] for part in parts for p in part.points]
    ys = [p[1] for part in parts for p in part.points]
    box = (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, 0.0, 0.0)
    return Shape(geometry, tuple(parts), vertex_count, box)


def rotate_points(points: Sequence[Pt], centre: Pt, degrees: float) -> list[Pt]:
    """Rotate about ``centre`` by the Qt convention (see the module docstring)."""
    if abs(degrees) < 1e-12:
        return [(float(x), float(y)) for x, y in points]
    rad = math.radians(degrees)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    cx, cy = centre
    return [
        (cx + (x - cx) * cos_a - (y - cy) * sin_a, cy + (x - cx) * sin_a + (y - cy) * cos_a)
        for x, y in points
    ]


def _bbox_centre(points: Sequence[Pt]) -> Pt:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)


def _pts(raw: object, what: str) -> list[Pt]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError(f"{what} is not a point list")
    out: list[Pt] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or "x" not in entry or "y" not in entry:
            raise ValueError(f"{what} holds a malformed point")
        out.append((float(entry["x"]), float(entry["y"])))
    return out


def _num(obj: Mapping[str, Any], key: str, default: float | None = None) -> float:
    value = obj.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"object has no usable {key!r}")
    return float(value)


def ellipse_perimeter(a: float, b: float) -> float:
    """Ramanujan's second approximation, as ``core.measurements`` uses for the canvas."""
    if a + b <= 0.0:
        return 0.0
    h = ((a - b) ** 2) / ((a + b) ** 2)
    return math.pi * (a + b) * (1 + (3 * h) / (10 + math.sqrt(4 - 3 * h)))


def _ellipse_ring(cx: float, cy: float, a: float, b: float, rotation_deg: float) -> list[Pt]:
    ring = [
        (
            cx + a * math.cos(2.0 * math.pi * i / CURVE_SEGMENTS),
            cy + b * math.sin(2.0 * math.pi * i / CURVE_SEGMENTS),
        )
        for i in range(CURVE_SEGMENTS)
    ]
    return rotate_points(ring, (cx, cy), rotation_deg)


def _bezier_points(obj: Mapping[str, Any]) -> list[Pt]:
    anchors = _pts(obj.get("anchors") or [], "anchors")
    h_in = _pts(obj.get("handles_in") or [], "handles_in")
    h_out = _pts(obj.get("handles_out") or [], "handles_out")
    if len(anchors) < 2 or len(h_in) != len(anchors) or len(h_out) != len(anchors):
        raise ValueError("bezier needs >= 2 anchors and matching handles")
    out = [anchors[0]]
    for i in range(1, len(anchors)):
        p0, p1, p2, p3 = anchors[i - 1], h_out[i - 1], h_in[i], anchors[i]
        for step in range(1, BEZIER_STEPS + 1):
            t = step / BEZIER_STEPS
            u = 1.0 - t
            out.append(
                (
                    u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
                    u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
                )
            )
    return out


def shape_from_object(obj: Mapping[str, Any]) -> Shape | None:
    """The object's geometry in scene cm after rotation, or ``None`` when it has none
    (a background image). Raises ``ValueError`` for an unmeasurable object."""
    kind = obj.get("type")
    rot = float(obj.get("rotation_angle") or 0.0) if kind != "group" else 0.0
    if kind == "background_image":
        return None
    if kind == "group":
        if abs(float(obj.get("rotation") or 0.0)) > 1e-6:
            raise ValueError("a rotated group has no measurable outline here; measure its members")
        gx, gy = _num(obj, "x", 0.0), _num(obj, "y", 0.0)
        parts: list[Part] = []
        for child in obj.get("children") or []:
            child_shape = shape_from_object(child)
            if child_shape is None:
                continue
            for part in child_shape.parts:
                parts.append(_translate(part, gx, gy))
        if not parts:
            raise ValueError("group has no measurable members")
        return _make_shape("group", parts, None)
    if kind == "rectangle":
        x, y, w, h = _num(obj, "x"), _num(obj, "y"), _num(obj, "width"), _num(obj, "height")
        ring = rotate_points(
            [(x, y), (x + w, y), (x + w, y + h), (x, y + h)], (x + w / 2, y + h / 2), rot
        )
        return _make_shape("rectangle", [Part("ring", tuple(ring))], 4)
    if kind in ("circle", "construction_circle"):
        cx, cy, r = _num(obj, "center_x"), _num(obj, "center_y"), _num(obj, "radius")
        ring = _ellipse_ring(cx, cy, r, r, 0.0)
        part = Part("ring", tuple(ring), area_cm2=math.pi * r * r,
                    length_cm=2 * math.pi * r, curved=True)
        return _make_shape("circle", [part], None)
    if kind == "ellipse":
        cx, cy = _num(obj, "center_x"), _num(obj, "center_y")
        a, b = _num(obj, "semi_x"), _num(obj, "semi_y")
        part = Part("ring", tuple(_ellipse_ring(cx, cy, a, b, rot)),
                    area_cm2=math.pi * a * b, length_cm=ellipse_perimeter(a, b), curved=True)
        return _make_shape("ellipse", [part], None)
    if kind == "polygon":
        points = _pts(obj.get("points"), "points")
        if len(points) < 3:
            raise ValueError("polygon has fewer than 3 vertices")
        centre = _bbox_centre(points)
        ring = rotate_points(points, centre, rot)
        holes = tuple(
            tuple(rotate_points(_pts(hole, "hole"), centre, rot)) for hole in obj.get("holes") or []
        )
        return _make_shape("polygon", [Part("ring", tuple(ring), holes)], len(points))
    if kind == "polyline":
        points = _pts(obj.get("points"), "points")
        if len(points) < 2:
            raise ValueError("polyline has fewer than 2 vertices")
        line = rotate_points(points, _bbox_centre(points), rot)
        return _make_shape("polyline", [Part("line", tuple(line))], len(points))
    if kind == "arc":
        cx, cy, r = _num(obj, "center_x"), _num(obj, "center_y"), _num(obj, "radius")
        start, span = _num(obj, "start_deg"), _num(obj, "span_deg")
        n = max(2, min(BEZIER_STEPS * 4, math.ceil(abs(span) / 2.0) + 1))
        pts = [
            (
                cx + r * math.cos(math.radians(start + span * i / (n - 1))),
                cy + r * math.sin(math.radians(start + span * i / (n - 1))),
            )
            for i in range(n)
        ]
        part = Part("line", tuple(pts), length_cm=r * abs(math.radians(span)), curved=True)
        return _make_shape("arc", [part], 2)
    if kind == "bezier":
        pts = _bezier_points(obj)
        return _make_shape(
            "bezier", [Part("line", tuple(pts), curved=True)], len(obj.get("anchors") or [])
        )
    if kind == "construction_line":
        p1 = (_num(obj, "x1"), _num(obj, "y1"))
        p2 = (_num(obj, "x2"), _num(obj, "y2"))
        return _make_shape("construction_line", [Part("line", (p1, p2))], 2)
    if kind in ("text", "journal_pin"):
        return _make_shape(str(kind), [Part("point", ((_num(obj, "x", 0.0), _num(obj, "y", 0.0)),))], 1)
    if kind == "callout":
        pt = (_num(obj, "target_x"), _num(obj, "target_y"))
        return _make_shape("callout", [Part("point", (pt,))], 1)
    raise ValueError(f"object type {kind!r} has no measurable geometry")


def _translate(part: Part, dx: float, dy: float) -> Part:
    def move(points: Sequence[Pt]) -> tuple[Pt, ...]:
        return tuple((x + dx, y + dy) for x, y in points)

    return Part(part.kind, move(part.points), tuple(move(h) for h in part.holes),
                part.area_cm2, part.length_cm, part.curved)


# --- area / length ----------------------------------------------------------


def ring_area(points: Sequence[Pt]) -> float:
    """Unsigned shoelace area (handles CW and CCW alike, as the canvas does)."""
    n = len(points)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def polyline_length(points: Sequence[Pt], closed: bool = False) -> float:
    return sum(leg_lengths(points, closed))


def leg_lengths(points: Sequence[Pt], closed: bool = False) -> list[float]:
    legs = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:], strict=False)]
    if closed and len(points) > 2:
        legs.append(math.hypot(points[0][0] - points[-1][0], points[0][1] - points[-1][1]))
    return legs


def part_perimeter(part: Part) -> float:
    return part.length_cm if part.length_cm is not None else polyline_length(part.points, True)


def area_and_perimeter(shape: Shape) -> tuple[float, float, int]:
    """``(area_cm2, outer_perimeter_cm, hole_count)`` of a single closed outline.
    Holes are subtracted from the area. ``ValueError`` for anything else."""
    if len(shape.parts) != 1 or shape.parts[0].kind != "ring":
        raise ValueError("area needs one closed outline (rectangle, polygon, circle or ellipse)")
    part = shape.parts[0]
    outer = part.area_cm2 if part.area_cm2 is not None else ring_area(part.points)
    holes = sum(ring_area(h) for h in part.holes)
    return max(outer - holes, 0.0), part_perimeter(part), len(part.holes)


def length_of(shape: Shape) -> tuple[float, bool]:
    """``(length_cm, closed)``: the path length of a line, or the perimeter of a closed shape."""
    if len(shape.parts) != 1 or shape.parts[0].kind == "point":
        raise ValueError("length needs one line or one closed outline")
    part = shape.parts[0]
    if part.kind == "ring":
        return part_perimeter(part), True
    if part.length_cm is not None:
        return part.length_cm, False
    return polyline_length(part.points), False


# --- distances --------------------------------------------------------------


def point_segment(p: Pt, a: Pt, b: Pt) -> tuple[float, Pt]:
    """Distance from ``p`` to segment ``ab`` and the nearest point on it."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    len_sq = dx * dx + dy * dy
    t = 0.0 if len_sq <= 0.0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len_sq))
    q = (a[0] + t * dx, a[1] + t * dy)
    return math.hypot(p[0] - q[0], p[1] - q[1]), q


def _cross(o: Pt, a: Pt, b: Pt) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def segment_segment(a1: Pt, a2: Pt, b1: Pt, b2: Pt) -> tuple[float, Pt, Pt, bool]:
    """``(distance, point_on_a, point_on_b, properly_crossed)`` between two segments."""
    d1, d2 = _cross(b1, b2, a1), _cross(b1, b2, a2)
    d3, d4 = _cross(a1, a2, b1), _cross(a1, a2, b2)
    if d1 * d2 < 0.0 and d3 * d4 < 0.0:
        t = d1 / (d1 - d2)
        x = (a1[0] + t * (a2[0] - a1[0]), a1[1] + t * (a2[1] - a1[1]))
        return 0.0, x, x, True
    best: tuple[float, Pt, Pt] | None = None
    for p, seg, flip in ((a1, (b1, b2), False), (a2, (b1, b2), False),
                         (b1, (a1, a2), True), (b2, (a1, a2), True)):
        dist, q = point_segment(p, seg[0], seg[1])
        cand = (dist, q, p) if flip else (dist, p, q)
        if best is None or cand[0] < best[0]:
            best = cand
    assert best is not None
    return best[0], best[1], best[2], False


def edges(shape: Shape) -> list[tuple[Pt, Pt]]:
    """Every boundary segment of the shape (rings closed, holes included)."""
    out: list[tuple[Pt, Pt]] = []
    for part in shape.parts:
        if part.kind == "point":
            out.append((part.points[0], part.points[0]))
            continue
        rings = [part.points, *part.holes] if part.kind == "ring" else [part.points]
        for ring in rings:
            n = len(ring)
            for i in range(n - 1):
                out.append((ring[i], ring[i + 1]))
            if part.kind == "ring" and n > 2:
                out.append((ring[-1], ring[0]))
    return out


def _in_ring(p: Pt, ring: Sequence[Pt]) -> bool:
    """Even-odd ray test (boundary points are classified arbitrarily; callers
    separate the boundary case with an edge distance)."""
    x, y = p
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _on_edges(p: Pt, segs: Sequence[tuple[Pt, Pt]]) -> bool:
    return any(point_segment(p, a, b)[0] <= TOUCH_TOLERANCE_CM for a, b in segs)


def strictly_inside(p: Pt, shape: Shape) -> bool:
    """True when ``p`` is in the interior of a ring part (not in a hole, not on an edge)."""
    for part in shape.parts:
        if part.kind != "ring":
            continue
        if _in_ring(p, part.points) and not any(_in_ring(p, h) for h in part.holes):
            return not _on_edges(p, edges(Shape("", (part,), None)))
    return False


@dataclass(frozen=True)
class PointToShape:
    distance_cm: float  # 0 when inside
    edge_distance_cm: float
    nearest: Pt
    inside: bool


def point_to_shape(p: Pt, shape: Shape) -> PointToShape:
    """Shortest distance from ``p`` to the shape's edge, and whether ``p`` is inside."""
    best = math.inf
    nearest = p
    for a, b in edges(shape):
        dist, q = point_segment(p, a, b)
        if dist < best:
            best, nearest = dist, q
    inside = strictly_inside(p, shape)
    return PointToShape(0.0 if inside else best, best, nearest, inside)


@dataclass(frozen=True)
class ClearanceResult:
    distance_cm: float
    point_a: Pt
    point_b: Pt
    relation: str  # "disjoint" | "touching" | "overlapping" | "contained"

    @property
    def overlaps(self) -> bool:
        return self.relation in ("overlapping", "contained")


def _box_gap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def _seg_box(s: tuple[Pt, Pt]) -> tuple[float, float, float, float]:
    return (min(s[0][0], s[1][0]), min(s[0][1], s[1][1]),
            max(s[0][0], s[1][0]), max(s[0][1], s[1][1]))


def clearance(a: Shape, b: Shape) -> ClearanceResult:
    """Minimum edge-to-edge distance between two shapes.

    ``0`` with relation ``overlapping`` when their edges cross, ``contained``
    when no edges cross but one shape lies inside the other's solid,
    ``touching`` when they share only boundary points. Two identical outlines
    share all boundary points and read as ``touching``.
    """
    segs_a, segs_b = edges(a), edges(b)
    boxes_b = [_seg_box(s) for s in segs_b]
    best = math.inf
    pa: Pt = segs_a[0][0]
    pb: Pt = segs_b[0][0]
    crossed = False
    for sa in segs_a:
        box_a = _seg_box(sa)
        if _box_gap(box_a, (b.bbox[0], b.bbox[1], b.bbox[2], b.bbox[3])) > best:
            continue
        for sb, box_b in zip(segs_b, boxes_b, strict=True):
            if _box_gap(box_a, box_b) > best:
                continue
            dist, qa, qb, cross = segment_segment(sa[0], sa[1], sb[0], sb[1])
            crossed = crossed or cross
            if dist < best:
                best, pa, pb = dist, qa, qb
    if crossed:
        return ClearanceResult(0.0, pa, pb, "overlapping")
    for src, dst in ((a, b), (b, a)):
        if not any(part.kind == "ring" for part in dst.parts):
            continue
        for part in src.parts:
            for p in part.points:
                if _box_gap((p[0], p[1], p[0], p[1]), dst.bbox) == 0.0 and strictly_inside(p, dst):
                    return ClearanceResult(0.0, p, p, "contained")
    if best <= TOUCH_TOLERANCE_CM:
        return ClearanceResult(0.0, pa, pb, "touching")
    return ClearanceResult(best, pa, pb, "disjoint")


# --- point-to-point, bearing, georeference ----------------------------------


def distance(a: Pt, b: Pt) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def plan_bearing_deg(a: Pt, b: Pt) -> float | None:
    """Direction a -> b clockwise from plan-up in ``[0, 360)``; ``None`` for a zero vector.

    Y-up scene: ``atan2(dx, dy)`` so +y is 0, +x is 90, -y is 180, -x is 270.
    """
    dx, dy = b[0] - a[0], b[1] - a[1]
    if math.hypot(dx, dy) < 1e-12:
        return None
    return math.degrees(math.atan2(dx, dy)) % 360.0


def compass_bearing_deg(a: Pt, b: Pt, north_angle_deg: float) -> float | None:
    """Compass bearing a -> b (0 north, 90 east) for a plan whose true north points
    ``north_angle_deg`` clockwise from plan-up (``core/plan_north``, ADR-037 addendum)."""
    plan = plan_bearing_deg(a, b)
    return None if plan is None else (plan - north_angle_deg) % 360.0


@dataclass(frozen=True)
class GeoLink:
    """A plan's georeference: scene origin (0, 0) is at (origin_lat, origin_lon)."""

    origin_lat: float
    origin_lon: float
    scale_m_per_unit: float = 0.01
    north_angle_deg: float = 0.0

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> GeoLink | None:
        raw = snapshot.get("georef")
        if not isinstance(raw, Mapping):
            return None
        try:
            lat, lon = float(raw["origin_lat"]), float(raw["origin_lon"])
            scale = float(raw.get("scale_meters_per_unit", 0.01))
            north = float(snapshot.get("north_angle_deg") or 0.0)
        except (KeyError, TypeError, ValueError):
            return None
        if not all(math.isfinite(v) for v in (lat, lon, scale, north)) or scale <= 0.0:
            return None
        return cls(lat, lon, scale, north % 360.0)

    def to_plan(self, lat: float, lon: float) -> Pt:
        """Scene point of a WGS-84 position (local equirectangular, as ``core.georef``)."""
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise ValueError("lat must be within [-90, 90] and lon within [-180, 180]")
        n = EARTH_RADIUS_M * math.radians(lat - self.origin_lat)
        dlon = (lon - self.origin_lon + 180.0) % 360.0 - 180.0
        e = EARTH_RADIUS_M * math.radians(dlon) * math.cos(math.radians(self.origin_lat))
        a = math.radians(self.north_angle_deg)
        x_m = e * math.cos(a) + n * math.sin(a)
        y_m = -e * math.sin(a) + n * math.cos(a)
        return x_m / self.scale_m_per_unit, y_m / self.scale_m_per_unit


def convex_extent(shapes: Sequence[Shape]) -> tuple[float, float, float, float] | None:
    """Union bounding box ``(min_x, min_y, max_x, max_y)`` of several shapes."""
    if not shapes:
        return None
    return (
        min(s.bbox[0] for s in shapes),
        min(s.bbox[1] for s in shapes),
        max(s.bbox[2] for s in shapes),
        max(s.bbox[3] for s in shapes),
    )
