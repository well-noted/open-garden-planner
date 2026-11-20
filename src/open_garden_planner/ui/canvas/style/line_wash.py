"""The Line & Wash (pen and watercolour) render style painter (Package 4, WP6).

Warm cold-press paper with a grain tile, hand-wobbled ink linework whose corners
overshoot, and washes of pigment laid slightly out of register with the ink. It draws:

* shapes (``paint_shape``): a wash (item colour mapped to a pigment, edge rings
  where the pigment pools, Multiply glazing and granulation on a raster painter),
  then ink (``wobble_polyline`` for corners, ``wobble_closed`` for curves);
* lines (``paint_path``): paths and fences from the existing walk geometry,
  half of the joints (a seeded sketch omits lines), irrigation as thin dashed ink;
* canopies (``paint_canopy``): overlapping tree and shrub canopies of one layer are
  one scalloped "cloud" mass with a shadow crescent (clusters are scene-wide and
  live in the ``StyleState``); each member paints its own part of the mass, so no
  item paints outside its own bounding rect;
* the paper (``paint_paper``) and the label hand (``label_font``).

Everything is a pure function of the item, the LOD band and the dpi; seeds are
``crc32`` of item id and role (no ``hash``, ``random`` or time: an AST test enforces
it). Washes and rings are true vector paths, so SVG and PDF carry the same geometry;
only Multiply, granulation and the grain are raster-only (design 2.5).
"""

from __future__ import annotations

import colorsys
import math
from contextvars import ContextVar
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Final

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QImage,
    QPaintEngine,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPolygonF,
    QTransform,
)
from PyQt6.QtWidgets import QGraphicsEllipseItem, QStyleOptionGraphicsItem

from open_garden_planner.core.object_types import PATH_FENCE_STYLES, PathFenceStyle
from open_garden_planner.core.render_style import RenderStyle
from open_garden_planner.core.render_style_roles import StyleRole, role_for_item
from open_garden_planner.ui.canvas.style.base import (
    STYLE_PAINTERS,
    CanopySpec,
    pen_for_painter,
    px_per_mm,
)
from open_garden_planner.ui.canvas.style.cad import (
    IRRIGATION_COLOR,
    _inset,
    _irrigation_ids,
    _mm_to_cm,
    _shape_of,
)
from open_garden_planner.ui.canvas.style.geometry import (
    cluster_circles,
    edge_ring,
    paper_mm_to_cm,
    scallop_outline,
    signed_area,
    style_seed,
    unit_hash,
    united_circles,
    wobble_closed,
    wobble_polyline,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Bump when the grain tile changes.
GRAIN_VERSION: Final = 1
GRAIN_SIZE: Final = 512

PAPER: Final = QColor("#f4eedf")
INK: Final = QColor(0x2A, 0x24, 0x1D, 217)  # #2a241d at 85 %

#: Ink weights, paper mm (design 2.3).
W_BUILDING: Final = 0.45
W_HARDSCAPE: Final = 0.30
W_PLANT: Final = 0.25
W_DETAIL: Final = 0.18

#: Wash alpha by role (default 0.5; design 2.3).
WASH_ALPHA: Final = {StyleRole.LAWN: 0.35, StyleRole.BUILDING: 0.30, StyleRole.WATER: 0.45}
WASH_ALPHA_DEFAULT: Final = 0.50
#: The wash used when an item has no colour of its own.
WASH_DEFAULT: Final = {
    StyleRole.BUILDING: "#9aa3ad",  # cool grey, always (design)
    StyleRole.ROOF: "#8f959c",
    StyleRole.HARDSCAPE: "#b8ad9c",
    StyleRole.PATH: "#b8ad9c",
    StyleRole.FENCE_WALL: "#a08c70",
    StyleRole.LAWN: "#7fae5a",
    StyleRole.BED: "#8a6a48",
    StyleRole.MULCH: "#7a5234",
    StyleRole.PLANT_TREE: "#5f9a52",
    StyleRole.PLANT_SHRUB: "#6fa85a",
    StyleRole.PLANT_GROUND: "#8bbf6a",
    StyleRole.WATER: "#5f9fd0",
    StyleRole.OTHER: "#b0a08a",
}
#: Pigment pooling rings: (width mm of paper, extra alpha at wash alpha 0.5).
EDGE_RINGS: Final = ((2.5, 0.12), (1.0, 0.25))
#: Misregistration of the wash against the ink, paper mm.
MISREGISTER_MM: Final = (0.6, 1.8)
#: The pad of ink beyond an item's own bounding rect, paper mm.
PAD_MM: Final = 4.0
PAD_PLANT_MM: Final = 8.0  # scallops bulge past the canopy circle
#: Granulation strength scale (design: 0.25 x (1 - lightness)).
GRANULATION: Final = 0.25
#: Most vertices the canopy masses of one state may hold; above it the arc
#: resample step doubles (design 2.6: 250 k total, here the canopy share).
MAX_STATE_VERTICES: Final = 120_000
MAX_DETAIL_ELEMENTS: Final = 3000
MAX_WOBBLE_POINTS: Final = 800
#: The hand of the labels (owner Q7), the app font in italic where it is missing.
LABEL_FAMILY: Final = "Segoe Print"

IRRIGATION_DASH_MM: Final = (4.0, 2.0)
#: Whether vector outputs (SVG/PDF) embed the paper grain (the opt-in checkbox of WP7).
PAPER_TEXTURE_IN_VECTOR: ContextVar[bool] = ContextVar("ogp_paper_texture_in_vector", default=False)

_VECTOR_ENGINES: Final = frozenset({
    QPaintEngine.Type.SVG, QPaintEngine.Type.Pdf, QPaintEngine.Type.User,
    QPaintEngine.Type.Picture, QPaintEngine.Type.Windows,
})


# ------------------------------------------------------------------ colour
def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    c = min(max(c, 0.0), 1.0)
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def pigment(color: QColor) -> QColor:
    """The pigment of ``color``: in linear light, saturation x 0.75 and
    lightness lifted 15 % toward white (design 2.3). Opaque; the caller sets alpha."""
    r, g, b = (_srgb_to_linear(v) for v in (color.redF(), color.greenF(), color.blueF()))
    h, light, sat = colorsys.rgb_to_hls(r, g, b)
    sat *= 0.75
    light = light + (1.0 - light) * 0.15
    r, g, b = colorsys.hls_to_rgb(h, light, sat)
    out = QColor()
    out.setRgbF(_linear_to_srgb(r), _linear_to_srgb(g), _linear_to_srgb(b))
    return out


def _with_alpha(color: QColor, alpha: float) -> QColor:
    out = QColor(color)
    out.setAlphaF(min(max(alpha, 0.0), 1.0))
    return out


# ------------------------------------------------------------------- paper
def _hash_u32(values: np.ndarray) -> np.ndarray:
    """Vectorised 32-bit finaliser (the same murmur3 mix as ``geometry``)."""
    v = values.astype(np.uint32)
    v ^= v >> np.uint32(16)
    v *= np.uint32(0x85EBCA6B)
    v ^= v >> np.uint32(13)
    v *= np.uint32(0xC2B2AE35)
    v ^= v >> np.uint32(16)
    return v


def _lattice_noise(size: int, cells: int, seed: int) -> np.ndarray:
    """Tileable value noise in [0, 1]: a ``cells`` x ``cells`` hashed lattice,
    smoothly interpolated over ``size`` x ``size`` pixels."""
    coords = np.arange(size, dtype=np.float64) * cells / size
    i0 = np.floor(coords).astype(np.int64)
    f = coords - i0
    f = f * f * (3.0 - 2.0 * f)
    i0 %= cells
    i1 = (i0 + 1) % cells
    lattice = _hash_u32(
        (np.arange(cells, dtype=np.uint32)[:, None] * np.uint32(73856093))
        ^ (np.arange(cells, dtype=np.uint32)[None, :] * np.uint32(19349663))
        ^ np.uint32(seed & 0xFFFFFFFF)
    ).astype(np.float64) / 4294967296.0
    top = lattice[i0][:, i0] * (1 - f)[None, :] + lattice[i0][:, i1] * f[None, :]
    bottom = lattice[i1][:, i0] * (1 - f)[None, :] + lattice[i1][:, i1] * f[None, :]
    return top * (1 - f)[:, None] + bottom * f[:, None]


@lru_cache(maxsize=1)
def grain_tile() -> QImage:
    """The paper grain: a 512 px tile, generated once per process.

    Four octaves of value noise plus a sparse fibre layer, from a fixed module seed
    (``GRAIN_VERSION``), so it is the same pixels in every process. Mostly
    transparent speckle (dark and light) to lay over the paper colour.
    """
    seed = style_seed("paper-grain", "paper", GRAIN_VERSION)
    noise = np.zeros((GRAIN_SIZE, GRAIN_SIZE))
    total = 0.0
    for octave in range(4):
        weight = 0.5**octave
        noise += weight * _lattice_noise(GRAIN_SIZE, 8 << octave, seed + octave)
        total += weight
    noise /= total
    noise = (noise - noise.min()) / max(float(np.ptp(noise)), 1e-9)
    # a hash speckle on top: the cold-press tooth
    flat = _hash_u32(np.arange(GRAIN_SIZE * GRAIN_SIZE, dtype=np.uint32) ^ np.uint32(seed))
    tooth = flat.astype(np.float64).reshape(GRAIN_SIZE, GRAIN_SIZE) / 4294967296.0
    value = 0.65 * noise + 0.35 * tooth
    dark = np.clip(0.5 - value, 0.0, 1.0)
    light = np.clip(value - 0.5, 0.0, 1.0)
    alpha = np.zeros((GRAIN_SIZE, GRAIN_SIZE))
    rgb = np.zeros((GRAIN_SIZE, GRAIN_SIZE, 3))
    rgb[:] = (118.0, 98.0, 68.0)
    alpha = dark * 0.22
    lit = light > dark
    rgb[lit] = (255.0, 252.0, 242.0)
    alpha = np.where(lit, light * 0.35, alpha)
    for i in range(120):  # fibres: short seeded strokes
        x = unit_hash(seed, 3 * i) * GRAIN_SIZE
        y = unit_hash(seed, 3 * i + 1) * GRAIN_SIZE
        angle = unit_hash(seed, 3 * i + 2) * math.pi
        for step in range(3 + i % 6):
            px = int(x + math.cos(angle) * step) % GRAIN_SIZE
            py = int(y + math.sin(angle) * step) % GRAIN_SIZE
            rgb[py, px] = (140.0, 118.0, 84.0)
            alpha[py, px] = max(alpha[py, px], 0.16)
    data = np.empty((GRAIN_SIZE, GRAIN_SIZE, 4), dtype=np.uint8)
    data[..., 0] = rgb[..., 2]  # Format_ARGB32 is B, G, R, A in memory
    data[..., 1] = rgb[..., 1]
    data[..., 2] = rgb[..., 0]
    data[..., 3] = np.clip(alpha * 255.0, 0, 255)
    image = QImage(data.tobytes(), GRAIN_SIZE, GRAIN_SIZE, GRAIN_SIZE * 4,
                   QImage.Format.Format_ARGB32)
    return image.copy()  # own the pixels (the buffer above is temporary)


def grain_brush(band: int) -> QBrush:
    """The grain as a brush anchored to scene (0, 0) at ``2 ** -band`` plan cm per
    tile pixel: one to two device pixels wide at any zoom inside the band, moving
    with the drawing."""
    brush = QBrush(grain_tile())
    scale = 2.0 ** (-band)
    brush.setTransform(QTransform.fromScale(scale, scale))
    return brush


def is_raster(painter: QPainter) -> bool:
    """Whether ``painter`` paints pixels (Multiply and the grain are raster-only;
    SVG, PDF and print paths are vector)."""
    engine = painter.paintEngine()
    return engine is None or engine.type() not in _VECTOR_ENGINES


# ----------------------------------------------------------------- helpers
def _band(item: Any) -> int:
    return int(getattr(item.scene(), "style_band", 0))


def _item_key(item: Any) -> str:
    key = getattr(item, "item_id_str", None)
    if key:
        return str(key)
    raw = getattr(item, "_item_id", None)
    return str(raw) if raw is not None else f"{type(item).__name__}"


def _role(item: Any) -> StyleRole:
    return role_for_item(item, _irrigation_ids(item.scene()))


def _ink(painter: QPainter, mm: float, color: QColor = INK) -> QPen:
    pen = pen_for_painter(painter, mm, color)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _dashed(pen: QPen, painter: QPainter, dash_mm: tuple[float, float]) -> QPen:
    width = max(pen.widthF(), 1e-6)
    per_mm = px_per_mm(painter)
    pen.setDashPattern([dash_mm[0] * per_mm / width, dash_mm[1] * per_mm / width])
    return pen


def _weight(role: StyleRole) -> float:
    if role in (StyleRole.BUILDING, StyleRole.ROOF):
        return W_BUILDING
    if role in (StyleRole.HARDSCAPE, StyleRole.PATH, StyleRole.FENCE_WALL):
        return W_HARDSCAPE
    if role in (StyleRole.PLANT_TREE, StyleRole.PLANT_SHRUB, StyleRole.PLANT_GROUND):
        return W_PLANT
    return W_DETAIL if role is StyleRole.LAWN else W_PLANT


def _wash_color(item: Any, role: StyleRole) -> QColor:
    base = None
    if role not in (StyleRole.BUILDING, StyleRole.ROOF):
        raw = getattr(item, "fill_color", None)
        if isinstance(raw, QColor) and raw.isValid():
            base = raw
    return pigment(base if base is not None else QColor(WASH_DEFAULT.get(role, "#b0a08a")))


def _subpaths(parts: Sequence[Sequence[QPointF]]) -> QPainterPath:
    path = QPainterPath()
    for part in parts:
        if len(part) < 2:
            continue
        path.moveTo(part[0])
        for point in part[1:]:
            path.lineTo(point)
    return path


def _polygons(path: QPainterPath) -> list[list[QPointF]]:
    """The flattened sub-paths of ``path`` as point lists (a closing duplicate dropped)."""
    out: list[list[QPointF]] = []
    for polygon in path.toSubpathPolygons():
        ring = [polygon.at(i) for i in range(polygon.count())]
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring.pop()
        if len(ring) >= 2:
            out.append(ring)
    return out


def _resample_ring(ring: Sequence[QPointF], step: float) -> list[QPointF]:
    """``ring`` as evenly spaced points about ``step`` apart (by arc length), so a
    scallop arc follows the ring and not the flattening of the curve it came from."""
    n = len(ring)
    lengths = [math.hypot(ring[(i + 1) % n].x() - ring[i].x(),
                          ring[(i + 1) % n].y() - ring[i].y()) for i in range(n)]
    perimeter = sum(lengths)
    if perimeter <= 1e-9:
        return list(ring)
    count = max(8, round(perimeter / max(step, 1e-6)))
    out: list[QPointF] = []
    edge, walked = 0, 0.0
    for k in range(count):
        target = perimeter * k / count
        while edge < n - 1 and walked + lengths[edge] < target:
            walked += lengths[edge]
            edge += 1
        t = 0.0 if lengths[edge] == 0.0 else (target - walked) / lengths[edge]
        a, b = ring[edge], ring[(edge + 1) % n]
        out.append(QPointF(a.x() + (b.x() - a.x()) * t, a.y() + (b.y() - a.y()) * t))
    return out


def _checksum(path: QPainterPath) -> tuple[int, float]:
    total = 0.0
    for i in range(path.elementCount()):
        e = path.elementAt(i)
        total += e.x * (i + 1) + e.y * (2 * i + 3)
    return path.elementCount(), round(total, 2)


def _dpi_key() -> float:
    from open_garden_planner.ui.canvas.style.base import OUTPUT_DPI
    return round(OUTPUT_DPI.get() or 0.0, 2)


def _memo(item: Any, attr: str, key: object, build: Any) -> Any:
    cached = getattr(item, attr, None)
    if cached is not None and cached[0] == key:
        return cached[1]
    value = build()
    setattr(item, attr, (key, value))
    return value


# ------------------------------------------------------------------ washes
def _paint_wash(
    painter: QPainter, wash: QPainterPath, color: QColor, alpha: float, band: int
) -> None:
    """The wash, its pooling rings and (raster) granulation. Washes glaze with
    Multiply on a raster painter; on a vector one they are plain alpha-over."""
    if wash.isEmpty():
        return
    raster = is_raster(painter)
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    if raster:
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
    painter.setBrush(_with_alpha(color, alpha))
    painter.drawPath(wash)
    for width_mm, extra in EDGE_RINGS:
        ring = edge_ring(wash, _mm_to_cm(width_mm, band))
        if not ring.isEmpty():
            painter.setBrush(_with_alpha(color, extra * alpha / WASH_ALPHA_DEFAULT))
            painter.drawPath(ring)
    if raster:
        lightness = color.lightnessF()
        painter.setClipPath(wash, Qt.ClipOperation.IntersectClip)
        painter.setOpacity(min(1.0, GRANULATION * (1.0 - lightness) * 4.0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.fillRect(wash.boundingRect(), grain_brush(band))
    painter.restore()


def _misregistration(seed: int, band: int) -> QPointF:
    angle = unit_hash(seed, 11) * 2.0 * math.pi
    mm = MISREGISTER_MM[0] + (MISREGISTER_MM[1] - MISREGISTER_MM[0]) * unit_hash(seed, 12)
    cm = _mm_to_cm(mm, band)
    return QPointF(math.cos(angle) * cm, math.sin(angle) * cm)


# ------------------------------------------------------------------ painter
class LineWashPainter:
    """The Line & Wash ``StylePainter`` (see the module docstring)."""

    #: The colour outputs and the canvas are filled with under the drawing.
    paper_color: QColor = PAPER

    # -- paper ------------------------------------------------------------
    def paint_paper(self, painter: QPainter, rect: QRectF, band: int) -> None:
        """The grain over the paper colour inside ``rect``: always on a raster
        painter, on SVG/PDF only when the output opted in (design 2.5)."""
        if not is_raster(painter) and not PAPER_TEXTURE_IN_VECTOR.get():
            return
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.fillRect(rect, grain_brush(band))
        painter.restore()

    # -- labels -----------------------------------------------------------
    def label_font(self, font: QFont) -> QFont:
        """``font`` in the hand of the style: Segoe Print where installed, else
        the same font in italic. The point size is untouched (an export sets it)."""
        out = QFont(font)
        if _has_label_family():
            out.setFamily(LABEL_FAMILY)
        else:
            out.setItalic(True)
        return out

    # -- shapes -----------------------------------------------------------
    def paint_shape(
        self, item: Any, painter: QPainter,
        option: QStyleOptionGraphicsItem,  # noqa: ARG002
    ) -> bool:
        geometry = _shape_of(item)
        if geometry is None:
            return False
        role = _role(item)
        if role is StyleRole.ANNOTATION:
            return False
        path, ring = geometry
        band = _band(item)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            if role is StyleRole.IRRIGATION:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(_dashed(
                    _ink(painter, W_PLANT, _with_alpha(IRRIGATION_COLOR, 0.7)),
                    painter, IRRIGATION_DASH_MM))
                painter.drawPath(path)
                return True
            seed = style_seed(_item_key(item), role.value)
            parts = _memo(
                item, "_lw_shape_cache",
                (tuple((round(p.x(), 3), round(p.y(), 3)) for p in ring), band,
                 _dpi_key(), role, isinstance(item, QGraphicsEllipseItem)),
                lambda: self._shape_geometry(item, path, ring, seed, band, role),
            )
            wash_path, ink_path, ripples = parts
            alpha = WASH_ALPHA.get(role, WASH_ALPHA_DEFAULT)
            _paint_wash(painter, wash_path, _wash_color(item, role), alpha, band)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if ripples:
                painter.setPen(_ink(painter, W_DETAIL, _with_alpha(INK, 0.5)))
                painter.drawPath(ripples)
            painter.setPen(_ink(painter, _weight(role)))
            painter.drawPath(ink_path)
        finally:
            painter.restore()
        return True

    def _shape_geometry(
        self, item: Any, path: QPainterPath, ring: Sequence[QPointF], seed: int,
        band: int, role: StyleRole,
    ) -> tuple[QPainterPath, QPainterPath, QPainterPath]:
        """(wash, ink, ripples) in item coordinates: the wash is the outline
        shifted by the seeded misregistration, the ink is wobbled."""
        shift = _misregistration(seed, band)
        wash = path.translated(shift)
        if isinstance(item, QGraphicsEllipseItem):
            ink = _subpaths([wobble_closed(ring, seed, band)])
        else:
            ink = _subpaths(wobble_polyline([*ring, ring[0]], seed, band))
        ripples = QPainterPath()
        if role is StyleRole.WATER:
            for step_mm in (1.4, 2.8):
                inner = _inset(path, _mm_to_cm(step_mm, band))
                if not inner.isEmpty():
                    ripples.addPath(inner)
        return wash, ink, ripples

    # -- lines ------------------------------------------------------------
    def paint_path(
        self, path_paint: Any, painter: QPainter, style: PathFenceStyle
    ) -> bool:
        owner = path_paint.owner
        if owner is None:  # the 3D ground bake: not a canvas paint
            return False
        band = _band(owner)
        role = _role(owner)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if role is StyleRole.IRRIGATION:
                painter.setPen(_dashed(
                    _ink(painter, W_PLANT, _with_alpha(IRRIGATION_COLOR, 0.7)),
                    painter, IRRIGATION_DASH_MM))
                painter.drawPath(path_paint.path())
                return True
            seed = style_seed(_item_key(owner), "line")
            if style is PathFenceStyle.NONE:
                painter.setPen(_ink(painter, W_HARDSCAPE))
                painter.drawPath(self._wobbled_centre(path_paint, owner, seed, band))
            else:
                self._styled_line(path_paint, owner, painter, style, seed, band)
        finally:
            painter.restore()
        return True

    def _wobbled_centre(self, path_paint: Any, owner: Any, seed: int, band: int) -> QPainterPath:
        path = path_paint.path()
        key = (_checksum(path), band)

        def build() -> QPainterPath:
            result = QPainterPath()
            for points in _polygons_open(path):
                if len(points) > MAX_WOBBLE_POINTS:
                    result.addPolygon(QPolygonF(points))
                else:
                    result.addPath(_subpaths(wobble_polyline(points, seed, band)))
            return result

        return _memo(owner, "_lw_centre_cache", key, build)

    def _ribbon(self, path_paint: Any, owner: Any, seed: int, band: int) -> tuple[QPainterPath, QPainterPath]:
        """(wash ribbon, wobbled outline) of the line at its drawn width."""
        path = path_paint.path()
        width = path_paint.pen().widthF()
        key = (_checksum(path), round(width, 3), band, _dpi_key())

        def build() -> tuple[QPainterPath, QPainterPath]:
            stroker = QPainterPathStroker()
            stroker.setWidth(width)
            stroker.setCapStyle(Qt.PenCapStyle.FlatCap)
            stroker.setJoinStyle(Qt.PenJoinStyle.BevelJoin)
            ribbon = stroker.createStroke(path)
            outline = QPainterPath()
            for ring in _polygons(ribbon):
                if 3 <= len(ring) <= MAX_WOBBLE_POINTS:
                    outline.addPath(_subpaths([wobble_closed(ring, seed, band)]))
                else:
                    outline.addPolygon(QPolygonF(ring))
            return ribbon.translated(_misregistration(seed, band)), outline

        return _memo(owner, "_lw_ribbon_cache", key, build)

    def _elements(self, path_paint: Any, spacing: float) -> list[tuple[int, Any]]:
        if spacing <= 0 or path_paint.path().length() / spacing > MAX_DETAIL_ELEMENTS:
            return []
        out: list[tuple[int, Any]] = []
        for index, element in path_paint._walk_indexed(spacing):
            out.append((index, element))
            if len(out) >= MAX_DETAIL_ELEMENTS:
                break
        return out

    def _styled_line(
        self, path_paint: Any, owner: Any, painter: QPainter, style: PathFenceStyle,
        seed: int, band: int,
    ) -> None:
        width = path_paint.pen().widthF()
        category = PATH_FENCE_STYLES[style].category
        is_path = category == "path"
        wash_role = StyleRole.PATH if is_path else StyleRole.FENCE_WALL
        if style is PathFenceStyle.HEDGE_FENCE:
            wash_role = StyleRole.PLANT_SHRUB
        ribbon, outline = self._ribbon(path_paint, owner, seed, band)
        if is_path or style in (PathFenceStyle.STONE_WALL, PathFenceStyle.HEDGE_FENCE):
            _paint_wash(painter, ribbon, _wash_color(owner, wash_role),
                        WASH_ALPHA_DEFAULT, band)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(_ink(painter, W_HARDSCAPE))
            painter.drawPath(outline)
        else:
            painter.setPen(_ink(painter, W_HARDSCAPE))
            painter.drawPath(self._wobbled_centre(path_paint, owner, seed, band))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        fine = _ink(painter, W_DETAIL)
        painter.setPen(fine)
        half = width / 2.0
        if is_path:
            if style in (PathFenceStyle.PAVED_PATH, PathFenceStyle.WOODEN_BOARDWALK):
                spacing = width * (0.8 if style is PathFenceStyle.PAVED_PATH else 0.25)
                for i, (pt, _dx, _dy, nx, ny) in self._elements(path_paint, spacing):
                    if unit_hash(seed, 100 + i) < 0.5:  # a sketch omits half the joints
                        continue
                    painter.drawLine(QPointF(pt.x() + nx * half, pt.y() + ny * half),
                                     QPointF(pt.x() - nx * half, pt.y() - ny * half))
            elif style is PathFenceStyle.STEPPING_STONES:
                stone_w, stone_h = width * 0.8, width * 0.6
                for _i, (pt, dx, dy, _nx, _ny) in self._elements(path_paint, width * 1.8):
                    painter.save()
                    painter.translate(pt.x(), pt.y())
                    painter.rotate(math.degrees(math.atan2(dy, dx)))
                    painter.drawRoundedRect(
                        QRectF(-stone_w / 2, -stone_h / 2, stone_w, stone_h), 3.0, 3.0)
                    painter.restore()
            else:  # gravel and dirt: sparse stipple
                dot = _ink(painter, W_DETAIL)
                painter.setPen(dot)
                for i, (pt, _dx, _dy, nx, ny) in self._elements(path_paint, width * 0.4):
                    if unit_hash(seed, 200 + i * 2) < 0.5:
                        across = (unit_hash(seed, 201 + i * 2) - 0.5) * width * 0.8
                        painter.drawPoint(QPointF(pt.x() + nx * across, pt.y() + ny * across))
            return
        if style in (PathFenceStyle.STONE_WALL, PathFenceStyle.HEDGE_FENCE):
            return
        # fences: posts as short ticks across the line
        post = max(width * 0.7, _mm_to_cm(1.4, band))
        spacing = max(width * 3.0, _mm_to_cm(10.0, band))
        painter.setPen(_ink(painter, W_PLANT))
        for _i, (pt, _dx, _dy, nx, ny) in self._elements(path_paint, spacing):
            painter.drawLine(QPointF(pt.x() + nx * post / 2, pt.y() + ny * post / 2),
                             QPointF(pt.x() - nx * post / 2, pt.y() - ny * post / 2))

    # -- canopies ---------------------------------------------------------
    def paint_canopy(self, item: Any, painter: QPainter, spec: Any) -> bool:
        if not isinstance(spec, CanopySpec) or spec.radius_cm <= 0:
            return False
        band = _band(item)
        role = _role(item)
        scene = item.scene()
        state = scene.style_state() if hasattr(scene, "style_state") else None
        entry = None
        if state is not None and role in (StyleRole.PLANT_TREE, StyleRole.PLANT_SHRUB):
            entry = state.data.get("canopy", {}).get(spec.item_id)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if entry is None:
                self._single_canopy(item, painter, spec, role, band)
            else:
                self._mass_part(item, painter, entry, spec, role, band)
        finally:
            painter.restore()
        return True

    def _single_canopy(
        self, item: Any, painter: QPainter, spec: CanopySpec, role: StyleRole, band: int
    ) -> None:
        """A plant outside any mass (or before the state exists): a wash disc and a
        wobbled ring; a tree gets its trunk dot."""
        c, r = spec.center, spec.radius_cm
        seed = style_seed(spec.item_id, "canopy")
        disc = QPainterPath()
        disc.addEllipse(c, r, r)
        wash_role = role if role in WASH_DEFAULT else StyleRole.PLANT_GROUND
        _paint_wash(painter, disc.translated(_misregistration(seed, band)),
                    _wash_color(item, wash_role), WASH_ALPHA_DEFAULT, band)
        ring = [QPointF(c.x() + r * math.cos(2 * math.pi * i / 32),
                        c.y() + r * math.sin(2 * math.pi * i / 32)) for i in range(32)]
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(_ink(painter, W_PLANT))
        painter.drawPath(_subpaths([wobble_closed(ring, seed, band)]))
        _trunk(painter, c, role)

    def _mass_part(
        self, item: Any, painter: QPainter, entry: dict[str, Any],
        spec: CanopySpec, role: StyleRole, band: int,
    ) -> None:
        region = item.mapFromScene(entry["region"])
        color = _wash_color(item, role)
        _paint_wash(painter, region, color, WASH_ALPHA_DEFAULT, band)
        crescent = item.mapFromScene(entry["crescent"])
        if not crescent.isEmpty():
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            if is_raster(painter):
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
            painter.setBrush(_with_alpha(color, 0.30))
            painter.drawPath(crescent)
            painter.restore()
        outline = item.mapFromScene(entry["outline"])
        if not outline.isEmpty():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(_ink(painter, W_PLANT))
            painter.drawPath(outline)
        _trunk(painter, spec.center, role)

    # -- the rest of the protocol -----------------------------------------
    def paint_label_decor(self, item: Any, painter: QPainter) -> None:  # noqa: ARG002
        return None

    def bounding_pad_cm(self, item: Any, band: int) -> float:
        role = _role(item)
        plant = role in (StyleRole.PLANT_TREE, StyleRole.PLANT_SHRUB, StyleRole.PLANT_GROUND)
        return _mm_to_cm(PAD_PLANT_MM if plant else PAD_MM, band)

    def build_state(self, scene: Any, band: int) -> dict[str, Any]:
        irrigation = _irrigation_ids(scene)
        return {"irrigation_layer_ids": irrigation, "canopy": _canopy_masses(scene, band, irrigation)}


# ----------------------------------------------------------------- helpers 2
@lru_cache(maxsize=1)
def _has_label_family() -> bool:
    return LABEL_FAMILY in QFontDatabase.families()


def _polygons_open(path: QPainterPath) -> list[list[QPointF]]:
    """Flattened sub-paths as open point lists (the closing duplicate is kept)."""
    out: list[list[QPointF]] = []
    for polygon in path.toSubpathPolygons():
        points = [polygon.at(i) for i in range(polygon.count())]
        if len(points) >= 2:
            out.append(points)
    return out


def _trunk(painter: QPainter, center: QPointF, role: StyleRole) -> None:
    """A small ink dot: a tree's trunk (a shrub's stem, smaller)."""
    dot = pen_for_painter(painter, 1.1 if role is StyleRole.PLANT_TREE else 0.7, INK)
    dot.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(dot)
    painter.drawPoint(center)


# ------------------------------------------------------------ canopy masses
def _canopy_masses(scene: Any, band: int, irrigation: frozenset[Any]) -> dict[str, dict[str, Any]]:
    """Cluster the tree and shrub canopies of each layer and cut every mass into
    the part each member paints (scene coordinates). Deterministic: members are
    ordered by item id, never by scene order."""
    members: dict[Any, list[tuple[str, float, float, float]]] = {}
    for item in scene.items():
        if not hasattr(item, "object_type") or not item.isVisible():
            continue
        role = role_for_item(item, irrigation)
        if role not in (StyleRole.PLANT_TREE, StyleRole.PLANT_SHRUB):
            continue
        rect = item.rect() if hasattr(item, "rect") else None
        if rect is None:
            continue
        diameter = rect.width()
        visual = getattr(item, "_visual_plant_diameter_cm", None)
        if visual is not None:
            diameter = float(visual(rect.width()))
        centre = item.mapToScene(rect.center())
        members.setdefault(getattr(item, "layer_id", None), []).append(
            (_item_key(item), centre.x(), centre.y(), diameter / 2.0 * item.scale()))
    result: dict[str, dict[str, Any]] = {}
    step_mm, vertices = 11.0, 0
    for _attempt in range(3):
        result.clear()
        vertices = 0
        for layer_members in members.values():
            layer_members.sort(key=lambda m: m[0])
            circles = [(x, y, r) for _k, x, y, r in layer_members]
            for cluster in cluster_circles(circles):
                vertices += _build_cluster(
                    result, [layer_members[i] for i in cluster], band, step_mm)
        if vertices <= MAX_STATE_VERTICES:
            break
        step_mm *= 2.0  # over budget: a coarser resample step (design 2.6)
    return result


def _build_cluster(
    out: dict[str, dict[str, Any]],
    cluster: list[tuple[str, float, float, float]],
    band: int,
    step_mm: float,
) -> int:
    circles = [(x, y, r) for _k, x, y, r in cluster]
    united = united_circles(circles)
    seed = style_seed(cluster[0][0], "mass")
    rings = [ring for ring in _polygons(united) if len(ring) >= 3]
    outline = QPainterPath()
    if rings:
        outer = max(rings, key=lambda ring: abs(signed_area(ring)))
        outline = scallop_outline(
            _resample_ring(outer, paper_mm_to_cm(step_mm, band)), seed, band)
    arcs = _split_arcs(outline, circles)
    running = QPainterPath()
    count = 0
    for index, (key, x, y, r) in enumerate(cluster):
        disc = QPainterPath()
        disc.addEllipse(QPointF(x, y), r, r)
        region = disc.subtracted(running) if not running.isEmpty() else disc
        running = disc if running.isEmpty() else running.united(disc)
        lit = QPainterPath()  # the lit disc shifted up-left (the plan is painted y-up)
        lit.addEllipse(QPointF(x - 0.28 * r, y + 0.28 * r), r, r)
        out[key] = {
            "region": region,
            "crescent": region.subtracted(lit),
            "outline": arcs[index],
            "cluster": seed,
        }
        count += arcs[index].elementCount()
    return count + outline.elementCount()


def _split_arcs(outline: QPainterPath, circles: Sequence[tuple[float, float, float]]) -> list[QPainterPath]:
    """Give every scallop arc of ``outline`` to the member whose canopy edge it lies
    on, so a member paints only its own stretch of the cloud (no ink twice, none
    outside its own bounds)."""
    parts = [QPainterPath() for _ in circles]
    if outline.isEmpty() or not circles:
        return parts
    last_owner = -1
    start = QPointF()
    i, count = 0, outline.elementCount()
    while i < count:
        e = outline.elementAt(i)
        if e.type == QPainterPath.ElementType.MoveToElement:
            start = QPointF(e.x, e.y)
            i += 1
        elif e.type == QPainterPath.ElementType.CurveToElement and i + 2 < count:
            c1, c2, end = outline.elementAt(i), outline.elementAt(i + 1), outline.elementAt(i + 2)
            mx = (start.x() + 3 * c1.x + 3 * c2.x + end.x) / 8.0
            my = (start.y() + 3 * c1.y + 3 * c2.y + end.y) / 8.0
            owner = min(range(len(circles)),
                        key=lambda k: (math.hypot(mx - circles[k][0], my - circles[k][1])
                                       - circles[k][2]))
            if owner != last_owner or parts[owner].isEmpty():
                parts[owner].moveTo(start)
            parts[owner].cubicTo(QPointF(c1.x, c1.y), QPointF(c2.x, c2.y), QPointF(end.x, end.y))
            last_owner = owner
            start = QPointF(end.x, end.y)
            i += 3
        else:
            i += 1
    return parts


def register() -> None:
    """Put the Line & Wash painter in ``STYLE_PAINTERS`` (idempotent)."""
    STYLE_PAINTERS[RenderStyle.LINE_AND_WASH] = LineWashPainter()


register()
