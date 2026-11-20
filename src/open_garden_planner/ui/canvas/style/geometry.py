"""Pure geometry for the render styles (Package 4, WP3).

Hatching, hand-drawn wobble, scalloped canopy outlines, edge-darkening rings
and canopy clustering. Qt types only: nothing here reads a scene, a settings
key or the clock, so the output is a pure function of its inputs. Two things
follow and are pinned by tests:

* the same input gives the same coordinates in any process, pan position or
  export (no ``hash()``, no ``random``, no time: an AST test enforces it);
* hatch lines are anchored to the scene origin, so they never crawl when a
  shape is moved or the view is panned.

Distances are plan centimetres. A line weight or spacing is given in paper
millimetres and converted with ``paper_mm_to_cm`` at the LOD ``band``
(``floor(log2(device px per cm))``): inside a band the geometry is identical,
so zooming shows no shimmer.
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Sequence

from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QPainterPath, QPainterPathStroker

#: Bump when the wobble algorithm changes; it is part of every seed.
GEOM_VERSION = 1

#: Device pixels per paper millimetre at 96 dpi.
_PX_PER_MM = 96.0 / 25.4

Segment = tuple[QPointF, QPointF]


# ---------------------------------------------------------------- units & seeds
def band_for_scale(device_px_per_cm: float) -> int:
    """LOD band: the zoom octave of ``device_px_per_cm``."""
    return math.floor(math.log2(max(device_px_per_cm, 1e-6)))


def paper_mm_to_cm(mm: float, band: int) -> float:
    """Plan centimetres that cover ``mm`` of paper at LOD ``band``."""
    return mm * _PX_PER_MM / (2.0**band)


def style_seed(item_id: str, role: str, version: int = GEOM_VERSION) -> int:
    """Stable 32-bit seed. ``crc32`` of a string: identical in every process."""
    return zlib.crc32(f"{item_id}:{role}:{version}".encode()) & 0xFFFFFFFF


def _mix(value: int) -> int:
    """32-bit integer finaliser (murmur3 style)."""
    value &= 0xFFFFFFFF
    value ^= value >> 16
    value = (value * 0x85EBCA6B) & 0xFFFFFFFF
    value ^= value >> 13
    value = (value * 0xC2B2AE35) & 0xFFFFFFFF
    value ^= value >> 16
    return value


def unit_hash(seed: int, index: int) -> float:
    """Deterministic value in [0, 1) for ``(seed, index)``."""
    return _mix(_mix(seed) ^ _mix(index + 0x9E3779B9)) / 4294967296.0


def value_noise_1d(x: float, seed: int) -> float:
    """Smooth 1-D value noise in [-1, 1], a pure function of ``x`` and ``seed``."""
    cell = math.floor(x)
    frac = x - cell
    fade = frac * frac * (3.0 - 2.0 * frac)
    a = unit_hash(seed, cell) * 2.0 - 1.0
    b = unit_hash(seed, cell + 1) * 2.0 - 1.0
    return a + (b - a) * fade


# -------------------------------------------------------------------- hatching
def hatch_segments(
    rings: Sequence[Sequence[QPointF]],
    angle_deg: float,
    spacing_cm: float,
    phase_origin: QPointF | None = None,
    max_segments: int | None = None,
) -> list[Segment] | None:
    """Exact scanline clip of a line family against a polygon.

    ``rings`` holds the outer ring and any holes; the even-odd rule decides
    what is inside, so holes and concave shapes clip exactly. The lines make
    ``angle_deg`` with the x axis and lie ``spacing_cm`` apart; their phase is
    anchored to ``phase_origin`` (the scene origin by default), so translating
    the polygon does not move them.

    Returns ``None`` when more than ``max_segments`` would be needed: the
    caller degrades to an outline plus a flat fill instead of stalling.
    """
    if spacing_cm <= 0:
        raise ValueError("spacing_cm must be positive")
    origin = phase_origin or QPointF(0.0, 0.0)
    theta = math.radians(angle_deg)
    dx, dy = math.cos(theta), math.sin(theta)
    nx, ny = -dy, dx
    phase = nx * origin.x() + ny * origin.y()

    # (offset0, along0, offset1, along1) of every edge in line-family coordinates.
    edges: list[tuple[float, float, float, float]] = []
    for ring in rings:
        count = len(ring)
        if count < 3:
            continue
        for i in range(count):
            p, q = ring[i], ring[(i + 1) % count]
            edges.append((
                nx * p.x() + ny * p.y(), dx * p.x() + dy * p.y(),
                nx * q.x() + ny * q.y(), dx * q.x() + dy * q.y(),
            ))
    if not edges:
        return []
    low = min(min(e[0], e[2]) for e in edges)
    high = max(max(e[0], e[2]) for e in edges)
    k_first = math.ceil((low - phase) / spacing_cm)
    k_last = math.floor((high - phase) / spacing_cm)
    if max_segments is not None and (k_last - k_first + 1) > max_segments:
        return None

    out: list[Segment] = []
    for k in range(k_first, k_last + 1):
        c = phase + k * spacing_cm
        hits: list[float] = []
        for o0, a0, o1, a1 in edges:
            # Half-open rule: a vertex exactly on the line counts once.
            if (o0 <= c) != (o1 <= c):
                t = (c - o0) / (o1 - o0)
                hits.append(a0 + t * (a1 - a0))
        hits.sort()
        for i in range(0, len(hits) - 1, 2):
            if hits[i + 1] - hits[i] <= 1e-9:
                continue
            out.append((
                QPointF(dx * hits[i] + nx * c, dy * hits[i] + ny * c),
                QPointF(dx * hits[i + 1] + nx * c, dy * hits[i + 1] + ny * c),
            ))
            if max_segments is not None and len(out) > max_segments:
                return None
    return out


# ---------------------------------------------------------------------- wobble
def _unit_normal(p: QPointF, q: QPointF) -> tuple[float, float, float]:
    """Unit normal ``(nx, ny)`` of the segment p->q and its length."""
    dx, dy = q.x() - p.x(), q.y() - p.y()
    length = math.hypot(dx, dy)
    if length < 1e-12:
        return 0.0, 0.0, 0.0
    return -dy / length, dx / length, length


def wobble_polyline(
    points: Sequence[QPointF],
    seed: int,
    band: int,
    *,
    step_mm: float = 4.0,
    amplitude_mm: float = 0.35,
    overshoot_mm: float = 1.2,
) -> list[list[QPointF]]:
    """Hand-drawn version of an open polyline: one open sub-path per segment.

    Each segment is resampled every ``step_mm`` of paper and displaced along
    its normal by value noise of the arc length (amplitude at most
    ``amplitude_mm`` and 1.5 % of the segment length), then extended past both
    vertices by up to ``overshoot_mm`` (and 6 % of its length) so corners cross
    like pen strokes. The noise depends only on ``seed`` and the arc length in
    plan cm, so the result is identical for the same plan in any pan or export.
    """
    step = max(paper_mm_to_cm(step_mm, band), 1e-6)
    amp_cap = paper_mm_to_cm(amplitude_mm, band)
    over_cap = paper_mm_to_cm(overshoot_mm, band)
    result: list[list[QPointF]] = []
    arc = 0.0
    for i in range(len(points) - 1):
        p, q = points[i], points[i + 1]
        nx, ny, length = _unit_normal(p, q)
        if length == 0.0:
            continue
        ux, uy = (q.x() - p.x()) / length, (q.y() - p.y()) / length
        amp = min(amp_cap, 0.015 * length)
        reach = min(over_cap, 0.06 * length)
        over_start = reach * (0.5 + 0.5 * unit_hash(seed, 2 * i))
        over_end = reach * (0.5 + 0.5 * unit_hash(seed, 2 * i + 1))
        count = max(1, round(length / step))
        stations = [length * j / count for j in range(count + 1)]
        sub: list[QPointF] = []
        for s in stations:
            offset = amp * value_noise_1d((arc + s) / (step * 3.0), seed)
            sub.append(QPointF(p.x() + ux * s + nx * offset, p.y() + uy * s + ny * offset))
        sub[0] = QPointF(sub[0].x() - ux * over_start, sub[0].y() - uy * over_start)
        sub[-1] = QPointF(sub[-1].x() + ux * over_end, sub[-1].y() + uy * over_end)
        result.append(sub)
        arc += length
    return result


def wobble_closed(
    points: Sequence[QPointF],
    seed: int,
    band: int,
    *,
    step_mm: float = 4.0,
    amplitude_mm: float = 0.35,
    overlap_fraction: float = 0.015,
) -> list[QPointF]:
    """Hand-drawn closed outline that does not quite close.

    The loop is resampled and displaced along its normal by low-frequency
    noise, then runs on past its start by about ``overlap_fraction`` of the
    perimeter with a small step off the first stroke, as a pen leaves a circle.
    Returns one open point list.
    """
    ring = list(points)
    n = len(ring)
    if n < 3:
        return ring
    lengths = [math.hypot(ring[(i + 1) % n].x() - ring[i].x(),
                          ring[(i + 1) % n].y() - ring[i].y()) for i in range(n)]
    perimeter = sum(lengths)
    if perimeter < 1e-9:
        return ring
    starts = [sum(lengths[:i]) for i in range(n)]
    step = max(paper_mm_to_cm(step_mm, band), 1e-6)
    amp = min(paper_mm_to_cm(amplitude_mm, band), 0.015 * perimeter)
    total = perimeter * (1.0 + overlap_fraction * (0.5 + 0.5 * unit_hash(seed, 7)))
    count = max(8, round(total / step))
    out: list[QPointF] = []
    for j in range(count + 1):
        s = total * j / count
        wrapped = s - perimeter if s > perimeter else s
        edge = n - 1
        for e in range(n):
            if starts[e] + lengths[e] >= wrapped:
                edge = e
                break
        p, q = ring[edge], ring[(edge + 1) % n]
        nx, ny, length = _unit_normal(p, q)
        t = 0.0 if length == 0.0 else (wrapped - starts[edge]) / length
        offset = amp * value_noise_1d(s / (step * 6.0), seed)
        if s > perimeter:  # the overlap runs a little off the first stroke
            offset += amp * 0.6
        out.append(QPointF(p.x() + (q.x() - p.x()) * t + nx * offset,
                           p.y() + (q.y() - p.y()) * t + ny * offset))
    return out


# -------------------------------------------------------------------- scallops
def signed_area(ring: Sequence[QPointF]) -> float:
    """Shoelace area: positive when the ring runs counter-clockwise in x-right, y-up."""
    total = 0.0
    for i in range(len(ring)):
        a, b = ring[i], ring[(i + 1) % len(ring)]
        total += a.x() * b.y() - b.x() * a.y()
    return total / 2.0


def scallop_outline(
    ring: Sequence[QPointF],
    seed: int,
    band: int,
    *,
    pitch_mm: tuple[float, float] = (8.0, 14.0),
    jitter: float = 0.2,
) -> QPainterPath:
    """Cloud-like canopy outline: outward arcs along a closed ring.

    The arc pitch is seeded between ``pitch_mm`` of paper and each arc's bulge
    is scaled by +/- ``jitter``. Bulges point away from the ring's interior
    whatever its winding.
    """
    path = QPainterPath()
    n = len(ring)
    if n < 3:
        return path
    # ``_unit_normal`` points to the left of travel: outward for a clockwise ring.
    outward = 1.0 if signed_area(ring) < 0 else -1.0
    pitch_lo, pitch_hi = (paper_mm_to_cm(v, band) for v in pitch_mm)
    path.moveTo(ring[0])
    arc_index = 0
    for i in range(n):
        p, q = ring[i], ring[(i + 1) % n]
        nx, ny, length = _unit_normal(p, q)
        if length == 0.0:
            continue
        pitch = pitch_lo + (pitch_hi - pitch_lo) * unit_hash(seed, 3 * i)
        arcs = max(1, round(length / pitch))
        span = length / arcs
        for a in range(arcs):
            t0, t1 = a / arcs, (a + 1) / arcs
            x0, y0 = p.x() + (q.x() - p.x()) * t0, p.y() + (q.y() - p.y()) * t0
            x1, y1 = p.x() + (q.x() - p.x()) * t1, p.y() + (q.y() - p.y()) * t1
            scale = 1.0 + jitter * (unit_hash(seed, 1000 + arc_index) * 2.0 - 1.0)
            lift = 0.6 * span * scale * outward  # control offset: apex = lift / 2
            path.quadTo((x0 + x1) / 2 + nx * lift, (y0 + y1) / 2 + ny * lift, x1, y1)
            arc_index += 1
    path.closeSubpath()
    return path


# ----------------------------------------------------------------- edge rings
def edge_ring(shape: QPainterPath, width_cm: float) -> QPainterPath:
    """The band of ``shape`` within ``width_cm`` of its edge (pigment pooling).

    True vector: ``shape`` intersected with its own stroke, no clip or blur, so
    raster, SVG and PDF agree.
    """
    stroker = QPainterPathStroker()
    stroker.setWidth(2.0 * width_cm)
    return shape.intersected(stroker.createStroke(shape))


# ------------------------------------------------------------------ clustering
def cluster_circles(
    circles: Sequence[tuple[float, float, float]], cell_cm: float | None = None
) -> list[list[int]]:
    """Group overlapping circles ``(x, y, r)`` into clusters of indices.

    Connected components of the "circles overlap" relation, found through a
    spatial grid so it stays near-linear. Clusters are sorted by their first
    index and indices ascend inside each, so the result is deterministic.
    """
    count = len(circles)
    if count == 0:
        return []
    cell = cell_cm or max(2.0 * max(r for _, _, r in circles), 1.0)
    grid: dict[tuple[int, int], list[int]] = {}
    for i, (x, y, _r) in enumerate(circles):
        grid.setdefault((math.floor(x / cell), math.floor(y / cell)), []).append(i)
    parent = list(range(count))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, (x, y, r) in enumerate(circles):
        cx, cy = math.floor(x / cell), math.floor(y / cell)
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for j in grid.get((gx, gy), ()):
                    if j <= i:
                        continue
                    xj, yj, rj = circles[j]
                    if math.hypot(x - xj, y - yj) < r + rj:
                        parent[find(j)] = find(i)
    groups: dict[int, list[int]] = {}
    for i in range(count):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=lambda g: g[0])


def united_circles(circles: Sequence[tuple[float, float, float]]) -> QPainterPath:
    """Union of circles as one path (a canopy mass)."""
    united = QPainterPath()
    for x, y, r in circles:
        one = QPainterPath()
        one.addEllipse(QPointF(x, y), r, r)
        united = one if united.isEmpty() else united.united(one)
    return united
